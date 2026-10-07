"""Model providers behind one neutral message format.

Neutral messages::

    {"role": "user", "content": str}
    {"role": "assistant", "content": str, "tool_calls": [ToolCall...], "raw": <provider blocks>}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str, "is_error": bool}

Anthropic goes through the official ``anthropic`` SDK; DeepSeek / Ollama /
OpenAI / vLLM go through the ``openai`` SDK against an OpenAI-compatible URL.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Response:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    usage: dict = field(default_factory=dict)
    raw: object = None
    reasoning: str = ""          # the model's thinking (OpenAI-compatible reasoning_content), sent back next turn


def parse_json(text: str):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError(f"no JSON in model output: {text[:200]}")
    return json.JSONDecoder().raw_decode(text[start:])[0]


class Provider:
    name = "base"

    def __init__(self, model: str, fast_model: str | None = None):
        self.model = model
        self.fast_model = fast_model or model
        self.usage = {"input": 0, "output": 0, "calls": 0}

    def context_window(self) -> int | None:
        """The model's context window in tokens, when the provider can tell."""
        return None

    def supports_vision(self) -> bool:
        """Whether the model can take images (tool results that show an image)."""
        return False

    def _count(self, i: int, o: int):
        self.usage["input"] += i
        self.usage["output"] += o
        self.usage["calls"] += 1

    def chat(self, system: str, messages: list[dict], tools: list[dict], effort: str = "high",
             max_tokens: int = 32000, fast: bool = False, thinking: int | None = None) -> Response:
        """``thinking``: this turn's reasoning-token budget, for servers that take one (rameness.thinking)."""
        raise NotImplementedError

    request_timeout: float | None = None      # per-call time budget (used by JEV escalations)

    def complete_json(self, system: str, prompt: str, max_tokens: int = 4000, timeout: float | None = None):
        prev, self.request_timeout = self.request_timeout, timeout
        try:
            # utility calls (structuring a trace, writing a small script) need little reasoning: without a cap a
            # local reasoning model thinks until the server's limit (measured: ~3 min per learned run on Qwen)
            r = self.chat(system, [{"role": "user", "content": prompt}], [], effort="low",
                          max_tokens=max_tokens, fast=True, thinking=1024)
        finally:
            self.request_timeout = prev
        return parse_json(r.text)


# --------------------------------------------------------------------------- Anthropic

class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model, fast_model=None, base_url=None, fallbacks=True):
        super().__init__(model, fast_model)
        import anthropic
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(base_url=base_url) if base_url else anthropic.Anthropic()

    def context_window(self) -> int | None:
        return 200000

    def supports_vision(self) -> bool:
        return True
        if not (self.client.api_key or self.client.auth_token):
            raise RuntimeError("no Anthropic credentials: set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN), "
                               "or run `ant auth login`, or pick another --provider")
        self.fallbacks = fallbacks

    @staticmethod
    def to_wire(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                if out and out[-1]["role"] == "user":          # e.g. a message injected after tool results
                    prev = out[-1]["content"]
                    blocks = prev if isinstance(prev, list) else [{"type": "text", "text": prev}]
                    out[-1]["content"] = blocks + [{"type": "text", "text": m["content"]}]
                else:
                    out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                if m.get("raw") is not None:
                    content = m["raw"]
                else:
                    content = ([{"type": "text", "text": m["content"]}] if m.get("content") else [])
                    content += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input}
                                for c in m.get("tool_calls", [])]
                out.append({"role": "assistant", "content": content})
            elif m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                if m.get("images"):
                    block["content"] = [{"type": "text", "text": m["content"]}] + [
                        {"type": "image", "source": {"type": "base64", "media_type": i["media_type"], "data": i["data"]}}
                        for i in m["images"]]
                if m.get("is_error"):
                    block["is_error"] = True
                # all results for one assistant turn go back in a single user message
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return out

    def _supports_effort(self, model: str) -> bool:
        return not model.startswith("claude-haiku")

    def chat(self, system, messages, tools, effort="high", max_tokens=32000, fast=False, thinking=None):
        model = self.fast_model if fast else self.model
        kw = dict(model=model, max_tokens=max_tokens, system=system, messages=self.to_wire(messages))
        if tools:
            kw["tools"] = tools
        if self._supports_effort(model):
            kw["thinking"] = {"type": "adaptive"}
            kw["output_config"] = {"effort": effort}
        use_fb = self.fallbacks and (model.startswith("claude-opus-5") or model.startswith("claude-fable"))
        client = self.client.with_options(timeout=self.request_timeout) if self.request_timeout else self.client
        try:
            if use_fb:
                with client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"],
                                                      fallbacks="default", **kw) as s:
                    msg = s.get_final_message()
            else:
                with client.messages.stream(**kw) as s:
                    msg = s.get_final_message()
        except self._anthropic.BadRequestError as e:
            if use_fb and "fallback" in str(e).lower():
                self.fallbacks = False          # endpoint/proxy doesn't accept it; stop trying
                return self.chat(system, messages, tools, effort, max_tokens, fast, thinking)
            raise
        self._count(msg.usage.input_tokens, msg.usage.output_tokens)
        text = "".join(b.text for b in msg.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in msg.content if b.type == "tool_use"]
        if msg.stop_reason == "refusal":
            text = text or "[the model declined this request]"
        raw = [b.model_dump(exclude_none=True) for b in msg.content]
        return Response(text, calls, msg.stop_reason or "", {"input": msg.usage.input_tokens,
                                                              "output": msg.usage.output_tokens}, raw)


# --------------------------------------------------------------------------- OpenAI-compatible

class OpenAICompatProvider(Provider):
    """DeepSeek, llama-server, Ollama, vLLM, LM Studio, OpenAI... anything speaking chat.completions.

    ``tool_mode``:
      native - use the server's tool calling
      prompt - describe tools in the system prompt and parse ``<tool_call>`` blocks from the reply text
               (for servers/models without tool-call templates; values are written raw, so code needs no
               JSON escaping)
      auto   - native, switching to prompt permanently if the server rejects ``tools``
    """

    name = "openai"
    _CALL = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)
    _FUNC = re.compile(r"<function=([\w.\-]+)>(.*?)(?:</function>|$)", re.S)
    _PARAM = re.compile(r"<parameter=([\w.\-]+)>\n?(.*?)\n?</parameter>", re.S)
    _THINK = re.compile(r"<think>.*?</think>\s*", re.S)

    def __init__(self, model, fast_model=None, base_url=None, api_key=None, reasoning_effort=False,
                 tool_mode="auto", timeout=3600):
        super().__init__(model, fast_model)
        from openai import OpenAI
        self.client = OpenAI(base_url=base_url, api_key=api_key or "not-needed", timeout=timeout)
        self.base_url = base_url
        self._window: int | None | bool = False
        self._props: dict | None = None
        self._model_card: dict | None = None
        self._budget_rejected = False          # the server refused the thinking-budget field once: stop sending it
        self.vision_failed = False             # set when the server failed a request because of its images
        # The message field the server returns the model's thinking in, and so reads it back from: llama.cpp,
        # TabbyAPI and ninfer use reasoning_content, current vLLM uses reasoning (and reads only that one back).
        self.reasoning_field = "reasoning_content"
        self.sampling: dict = {}               # per-request sampling (sampling_for); empty: the server's defaults
        self.reasoning_effort = reasoning_effort
        self.tool_mode = tool_mode

    def _server_props(self) -> dict:
        """llama-server's /props (window, modalities); {} for servers without it."""
        if self._props is None:
            self._props = {}
            if self.base_url:
                import urllib.request
                root = self.base_url.rstrip("/").removesuffix("/v1")
                try:
                    with urllib.request.urlopen(root + "/props", timeout=5) as r:
                        self._props = json.load(r)
                except Exception:
                    pass
        return self._props

    def _models_entry(self) -> dict:
        """This model's entry in the server's /v1/models list (vLLM and ninfer report max_model_len there, and
        say who serves it in owned_by); {} when the server does not answer."""
        if self._model_card is None:
            self._model_card = {}
            if self.base_url:
                import urllib.request
                try:
                    with urllib.request.urlopen(self.base_url.rstrip("/") + "/models", timeout=5) as r:
                        data = json.load(r).get("data") or []
                    self._model_card = next((m for m in data if m.get("id") == self.model), data[0] if data else {})
                except Exception:
                    pass
        return self._model_card

    def server_kind(self) -> str:
        """llama (answers llama.cpp's /props), vllm, ninfer, tabby, or unknown (a hosted API, or anything else)."""
        p = self._server_props()
        if (p.get("default_generation_settings") or {}).get("n_ctx") or p.get("n_ctx"):
            return "llama"
        owner = str(self._models_entry().get("owned_by") or "").lower()
        return {"vllm": "vllm", "ninfer": "ninfer", "tabbyapi": "tabby"}.get(owner, "unknown")

    # The request field each server reads a per-request thinking budget from. Unknown servers get none: a strict
    # API may reject a field it does not know.
    BUDGET_FIELDS = {"llama": "reasoning_budget_tokens", "tabby": "reasoning_budget_tokens",
                     "vllm": "thinking_token_budget", "ninfer": "thinking_budget"}

    def budget_field(self) -> str | None:
        return None if self._budget_rejected else self.BUDGET_FIELDS.get(self.server_kind())

    def context_window(self) -> int | None:
        """llama-server reports its per-slot window at /props (n_ctx); vLLM and ninfer report max_model_len in
        /v1/models; other servers: unknown."""
        if self._window is False:
            p = self._server_props()
            n = (p.get("default_generation_settings") or {}).get("n_ctx") or p.get("n_ctx")
            if not n:
                card = self._models_entry()
                n = card.get("max_model_len") or card.get("context_length")
            self._window = int(n) if n else None
        return self._window

    def supports_vision(self) -> bool:
        """llama-server says so at /props (modalities.vision: it was started with a vision projector)."""
        if self.vision_failed:
            return False
        if (self._server_props().get("modalities") or {}).get("vision"):
            return True
        # ninfer (and llama.cpp's router mode) list input modalities in /v1/models
        return "image" in ((self._models_entry().get("architecture") or {}).get("input_modalities") or [])

    @staticmethod
    def to_wire(system: str, messages: list[dict], prompted: bool = False,
                reasoning_field: str = "reasoning_content") -> list[dict]:
        out = [{"role": "system", "content": system}] if system else []
        shown: list[dict] = []                 # images from the tool results just added

        def show():
            # chat.completions tool messages are text only: the images follow the results as a user message
            if shown:
                out.append({"role": "user", "content": [{"type": "text", "text": "[images from the tool results above]"}]
                            + [{"type": "image_url", "image_url": {"url": f"data:{i['media_type']};base64,{i['data']}"}}
                               for i in shown]})
                shown.clear()

        for m in messages:
            if m["role"] != "tool":
                show()
            shown.extend(m.get("images") or [])
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                if prompted:
                    text = (m.get("content") or "") + "".join(
                        "\n" + OpenAICompatProvider.format_call(c.name, c.input) for c in m.get("tool_calls", []))
                    w = {"role": "assistant", "content": text}
                    if m.get("reasoning"):
                        w[reasoning_field] = m["reasoning"]
                    out.append(w)
                    continue
                w = {"role": "assistant", "content": m.get("content") or None}
                if m.get("reasoning"):
                    # keep the model's own reasoning in the history, as Claude Code does with thinking blocks:
                    # without it the model re-derives its plan, and forgets what it ruled out, every turn
                    w[reasoning_field] = m["reasoning"]
                if m.get("tool_calls"):
                    w["tool_calls"] = [{"id": c.id, "type": "function",
                                        "function": {"name": c.name, "arguments": json.dumps(c.input)}}
                                       for c in m["tool_calls"]]
                out.append(w)
            elif m["role"] == "tool":
                if prompted:
                    block = f"<tool_result name=\"{m.get('name', '')}\">\n{m['content']}\n</tool_result>"
                    if out and out[-1]["role"] == "user" and out[-1]["content"].startswith("<tool_result"):
                        out[-1]["content"] += "\n" + block
                    else:
                        out.append({"role": "user", "content": block})
                else:
                    out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        show()
        return out

    @staticmethod
    def format_call(name: str, args: dict) -> str:
        """The text form of a call: one <parameter> block per argument, values written raw - code and file
        contents need no JSON escaping (the format Qwen3-Coder-style models are trained on)."""
        params = "".join(f"<parameter={k}>\n{v if isinstance(v, str) else json.dumps(v)}\n</parameter>\n"
                         for k, v in args.items())
        return f"<tool_call>\n<function={name}>\n{params}</function>\n</tool_call>"

    @staticmethod
    def tool_prompt(tools: list[dict]) -> str:
        lines = ["# Tools", "Call a tool by writing a block like this (one <parameter> per argument; write values as-is, "
                 "no quoting or escaping; numbers, booleans and lists as JSON):",
                 OpenAICompatProvider.format_call("tool_name", {"arg": "value", "other_arg": "multi-line\nvalue"}),
                 "You may make several calls in one reply; stop after your last call. The results come back in "
                 "<tool_result> blocks. When the task is complete, reply without any tool_call block.", ""]
        for t in tools:
            lines.append(f"## {t['name']}\n{t['description']}\nparameters: {json.dumps(t['input_schema'])}")
        return "\n".join(lines)

    @staticmethod
    def _typed(v: str, schema: dict):
        """Parameter text -> the type the tool's schema asks for (strings stay exactly as written)."""
        if (schema or {}).get("type", "string") == "string":
            return v
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v

    def _parse_prompted(self, text: str, tools: list[dict] | None = None) -> tuple[str, list[ToolCall]]:
        """Both text forms: <function=..><parameter=..> blocks, and a JSON object {"name", "arguments"}."""
        props = {t["name"]: (t.get("input_schema") or {}).get("properties") or {} for t in tools or []}
        calls = []
        for i, m in enumerate(self._CALL.finditer(text)):
            body = m.group(1)
            cid = f"call_{self.usage['calls']}_{i}"
            f = self._FUNC.search(body)
            if f:
                name = f.group(1)
                args = {k: self._typed(v, props.get(name, {}).get(k)) for k, v in self._PARAM.findall(f.group(2))}
                calls.append(ToolCall(cid, name, args))
                continue
            try:
                d = json.loads(body)
                calls.append(ToolCall(cid, d["name"], d.get("arguments") or {}))
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        return self._CALL.sub("", text).strip(), calls

    def chat(self, system, messages, tools, effort="high", max_tokens=32000, fast=False, thinking=None):
        model = self.fast_model if fast else self.model
        prompted = bool(tools) and self.tool_mode == "prompt"
        sys_text = system + ("\n\n" + self.tool_prompt(tools) if prompted else "")
        kw = dict(model=model, messages=self.to_wire(sys_text, messages, prompted, self.reasoning_field),
                  max_tokens=max_tokens)
        if prompted:
            kw["stop"] = ["<tool_result"]          # the results are ours to write, not the model's
        if tools and not prompted:
            kw["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                             "parameters": t["input_schema"]}} for t in tools]
        if self.reasoning_effort:
            kw["reasoning_effort"] = effort if effort in ("low", "medium", "high") else "high"
        extra = {}
        for k, v in self.sampling.items():
            if k in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
                kw[k] = v                      # standard OpenAI parameters
            else:
                extra[k] = v                   # top_k, min_p, chat_template_kwargs: server extensions
        field = self.budget_field() if thinking is not None else None
        if field:
            # a per-request cap on reasoning tokens, in the field this server reads it from
            extra[field] = int(thinking)
        if extra:
            kw["extra_body"] = extra
        client = self.client.with_options(timeout=self.request_timeout, max_retries=0) if self.request_timeout else self.client
        try:
            r = client.chat.completions.create(**kw)
        except Exception as e:
            from openai import BadRequestError, InternalServerError
            if field and isinstance(e, BadRequestError) and field in str(e):
                # the server does not take a thinking budget after all (e.g. no reasoning parser configured):
                # stop sending it rather than fail the run
                self._budget_rejected = True
                return self.chat(system, messages, tools, effort, max_tokens, fast, thinking)
            if (tools and not prompted and self.tool_mode == "auto"
                    and isinstance(e, (BadRequestError, InternalServerError)) and "tool" in str(e).lower()):
                self.tool_mode = "prompt"
                return self.chat(system, messages, tools, effort, max_tokens, fast, thinking)
            if isinstance(e, (BadRequestError, InternalServerError)) and any(m.get("images") for m in messages):
                # a server can advertise vision and still fail on images (e.g. llama-server with a speculative draft
                # model that has no vision): drop the images, say so, and stop sending them for this session
                self.vision_failed = True
                for m in messages:
                    if m.pop("images", None):
                        m["content"] = f"{m['content']} [not shown: the model server failed on images, so they are off]"
                return self.chat(system, messages, tools, effort, max_tokens, fast, thinking)
            raise
        ch = r.choices[0]
        u = r.usage
        self._count(getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0)
        raw_text = ch.message.content or ""
        reasoning = ""
        for field in ("reasoning_content", "reasoning"):
            value = getattr(ch.message, field, None) or (ch.message.model_extra or {}).get(field)
            if isinstance(value, str) and value:
                reasoning, self.reasoning_field = value, field
                break
        reasoning = reasoning or "\n".join(re.findall(r"<think>(.*?)</think>", raw_text, re.S))
        text = self._THINK.sub("", raw_text)
        calls = []
        for tc in ch.message.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_invalid_json": tc.function.arguments}
            calls.append(ToolCall(tc.id, tc.function.name, args))
        if not calls and "<tool_call>" in text and tools:     # model used the text protocol anyway
            text, calls = self._parse_prompted(text, tools)
        stop = "tool_use" if calls else ("max_tokens" if ch.finish_reason == "length" else "end_turn")
        return Response(text, calls, stop,
                        {"input": getattr(u, "prompt_tokens", 0), "output": getattr(u, "completion_tokens", 0)},
                        reasoning=(reasoning or "").strip())


# --------------------------------------------------------------------------- scripted (tests / demos)

class FakeProvider(Provider):
    """Replays scripted responses. Each script item is a Response or a callable(messages)->Response."""

    name = "fake"

    def __init__(self, script=None, json_script=None):
        super().__init__("fake", "fake")
        self.script = list(script or [])
        self.json_script = list(json_script or [])
        self.seen: list[dict] = []

    def chat(self, system, messages, tools, effort="high", max_tokens=32000, fast=False, thinking=None):
        self.seen.append({"system": system, "messages": list(messages), "tools": [t["name"] for t in tools],
                          "effort": effort})
        self._count(sum(len(str(m.get("content", ""))) for m in messages) // 4, 10)
        if not self.script:
            return Response("done", [], "end_turn")
        item = self.script.pop(0)
        return item(messages) if callable(item) else item

    def complete_json(self, system, prompt, max_tokens=4000, timeout=None):
        self._count(len(prompt) // 4, 10)
        if not self.json_script:
            raise RuntimeError("FakeProvider: no scripted JSON")
        item = self.json_script.pop(0)
        return item(prompt) if callable(item) else item


# Vendor-recommended sampling per open-weight model family (from the model cards), used when "sampling" is
# "auto". Servers often run with other defaults (a lower temperature, no presence penalty), and agentic models
# that were trained to keep their reasoning across turns are told to (preserve_thinking).
SAMPLING_PROFILES = {
    # occamy's card says temp 1.0 / presence 1.5, but on long agentic builds (Rameness, same code, paired runs)
    # temp 0.6 / presence 0 scored 78.6 vs 64.3 and 42.9 vs 21.4, and decoded faster (~24 vs ~20 tok/s: a
    # speculative draft is accepted more often at lower temperature). Override with config "sampling".
    "occamy": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 0.0,
               "chat_template_kwargs": {"enable_thinking": True, "preserve_thinking": True}},
    "qwen3": {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 0.0,
              "chat_template_kwargs": {"enable_thinking": True, "preserve_thinking": True}},
}


def sampling_for(model: str | None, setting) -> dict:
    """``setting``: "auto" (the matching profile, if any), "server" (send nothing), or explicit params."""
    if isinstance(setting, dict):
        return dict(setting)
    if setting != "auto" or not model:
        return {}
    m = model.lower()
    for family, params in SAMPLING_PROFILES.items():
        if family in m:
            return dict(params)
    return {}


def build(cfg: dict) -> Provider | None:
    p = cfg["provider"]
    key = os.environ.get(cfg["api_key_env"]) if cfg.get("api_key_env") else None
    if p == "fake":
        return FakeProvider()
    if p == "anthropic":
        return AnthropicProvider(cfg["model"], cfg["fast_model"], cfg.get("base_url"), cfg["anthropic_fallbacks"])
    prov = OpenAICompatProvider(cfg["model"], cfg["fast_model"], cfg.get("base_url"), key,
                                reasoning_effort=cfg.get("reasoning_effort", False),
                                tool_mode=cfg.get("tool_mode", "auto"), timeout=cfg.get("request_timeout") or 3600)
    prov.sampling = sampling_for(cfg["model"], cfg.get("sampling", "auto"))
    return prov
