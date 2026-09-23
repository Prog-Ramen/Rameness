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

    def _count(self, i: int, o: int):
        self.usage["input"] += i
        self.usage["output"] += o
        self.usage["calls"] += 1

    def chat(self, system: str, messages: list[dict], tools: list[dict], effort: str = "high",
             max_tokens: int = 32000, fast: bool = False) -> Response:
        raise NotImplementedError

    request_timeout: float | None = None      # per-call time budget (used by JEV escalations)

    def complete_json(self, system: str, prompt: str, max_tokens: int = 4000, timeout: float | None = None):
        prev, self.request_timeout = self.request_timeout, timeout
        try:
            r = self.chat(system, [{"role": "user", "content": prompt}], [], effort="low",
                          max_tokens=max_tokens, fast=True)
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

    def chat(self, system, messages, tools, effort="high", max_tokens=32000, fast=False):
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
                return self.chat(system, messages, tools, effort, max_tokens, fast)
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
      prompt - describe tools in the system prompt and parse ``<tool_call>{json}</tool_call>`` blocks
               (for local servers/models without tool-call templates)
      auto   - native, switching to prompt permanently if the server rejects ``tools``
    """

    name = "openai"
    _CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
    _THINK = re.compile(r"<think>.*?</think>\s*", re.S)

    def __init__(self, model, fast_model=None, base_url=None, api_key=None, reasoning_effort=False,
                 tool_mode="auto", timeout=600):
        super().__init__(model, fast_model)
        from openai import OpenAI
        self.client = OpenAI(base_url=base_url, api_key=api_key or "not-needed", timeout=timeout)
        self.reasoning_effort = reasoning_effort
        self.tool_mode = tool_mode

    @staticmethod
    def to_wire(system: str, messages: list[dict], prompted: bool = False) -> list[dict]:
        out = [{"role": "system", "content": system}] if system else []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                if prompted:
                    text = (m.get("content") or "") + "".join(
                        f"\n<tool_call>{json.dumps({'name': c.name, 'arguments': c.input})}</tool_call>"
                        for c in m.get("tool_calls", []))
                    out.append({"role": "assistant", "content": text})
                    continue
                w = {"role": "assistant", "content": m.get("content") or None}
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
        return out

    @staticmethod
    def tool_prompt(tools: list[dict]) -> str:
        lines = ["# Tools", "Call a tool by writing exactly one block per call:",
                 '<tool_call>{"name": "<tool>", "arguments": {...}}</tool_call>',
                 "You may make several calls in one reply. Results come back in <tool_result> blocks. "
                 "When the task is complete, reply without any tool_call block.", ""]
        for t in tools:
            lines.append(f"## {t['name']}\n{t['description']}\nparameters: {json.dumps(t['input_schema'])}")
        return "\n".join(lines)

    def _parse_prompted(self, text: str) -> tuple[str, list[ToolCall]]:
        calls = []
        for i, m in enumerate(self._CALL.finditer(text)):
            try:
                d = json.loads(m.group(1))
                calls.append(ToolCall(f"call_{self.usage['calls']}_{i}", d["name"], d.get("arguments") or {}))
            except (json.JSONDecodeError, KeyError):
                continue
        return self._CALL.sub("", text).strip(), calls

    def chat(self, system, messages, tools, effort="high", max_tokens=32000, fast=False):
        model = self.fast_model if fast else self.model
        prompted = bool(tools) and self.tool_mode == "prompt"
        sys_text = system + ("\n\n" + self.tool_prompt(tools) if prompted else "")
        kw = dict(model=model, messages=self.to_wire(sys_text, messages, prompted), max_tokens=min(max_tokens, 8192))
        if tools and not prompted:
            kw["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                             "parameters": t["input_schema"]}} for t in tools]
        if self.reasoning_effort:
            kw["reasoning_effort"] = effort if effort in ("low", "medium", "high") else "high"
        client = self.client.with_options(timeout=self.request_timeout, max_retries=0) if self.request_timeout else self.client
        try:
            r = client.chat.completions.create(**kw)
        except Exception as e:
            from openai import BadRequestError, InternalServerError
            if (tools and not prompted and self.tool_mode == "auto"
                    and isinstance(e, (BadRequestError, InternalServerError)) and "tool" in str(e).lower()):
                self.tool_mode = "prompt"
                return self.chat(system, messages, tools, effort, max_tokens, fast)
            raise
        ch = r.choices[0]
        u = r.usage
        self._count(getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0)
        text = self._THINK.sub("", ch.message.content or "")
        calls = []
        for tc in ch.message.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_invalid_json": tc.function.arguments}
            calls.append(ToolCall(tc.id, tc.function.name, args))
        if not calls and "<tool_call>" in text and tools:     # model used the text protocol anyway
            text, calls = self._parse_prompted(text)
        stop = "tool_use" if calls else ("max_tokens" if ch.finish_reason == "length" else "end_turn")
        return Response(text, calls, stop,
                        {"input": getattr(u, "prompt_tokens", 0), "output": getattr(u, "completion_tokens", 0)})


# --------------------------------------------------------------------------- scripted (tests / demos)

class FakeProvider(Provider):
    """Replays scripted responses. Each script item is a Response or a callable(messages)->Response."""

    name = "fake"

    def __init__(self, script=None, json_script=None):
        super().__init__("fake", "fake")
        self.script = list(script or [])
        self.json_script = list(json_script or [])
        self.seen: list[dict] = []

    def chat(self, system, messages, tools, effort="high", max_tokens=32000, fast=False):
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


def build(cfg: dict) -> Provider | None:
    p = cfg["provider"]
    key = os.environ.get(cfg["api_key_env"]) if cfg.get("api_key_env") else None
    if p == "fake":
        return FakeProvider()
    if p == "anthropic":
        return AnthropicProvider(cfg["model"], cfg["fast_model"], cfg.get("base_url"), cfg["anthropic_fallbacks"])
    return OpenAICompatProvider(cfg["model"], cfg["fast_model"], cfg.get("base_url"), key,
                                reasoning_effort=cfg.get("reasoning_effort", False),
                                tool_mode=cfg.get("tool_mode", "auto"))
