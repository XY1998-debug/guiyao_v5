"""统一 LLM 客户端 - 支持 OpenAI 和 Anthropic 两种协议（移植自 V1）"""

import json
import os
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("quantpilot.client")


@dataclass
class StreamChunk:
    type: str  # "thinking" / "content" / "tool_calls" / "done"
    text: str = ""
    tool_calls: list = field(default_factory=list)


class LLMClient:
    """统一 LLM 客户端"""

    def __init__(self, config: dict, model_key: str = "primary"):
        llm_config = config["llm"][model_key]
        self.model = llm_config["model"]
        self.protocol = llm_config.get("protocol", "openai")
        self.max_tokens = llm_config.get("max_tokens", 8192)
        self.temperature = llm_config.get("temperature", 0.3)
        self.timeout = llm_config.get("timeout", 300)
        self._init_client(llm_config)

    def _init_client(self, llm_config: dict):
        if self.protocol == "openai":
            from openai import OpenAI, AsyncOpenAI
            self.client = OpenAI(
                api_key=llm_config["api_key"],
                base_url=llm_config["base_url"],
            )
            self.async_client = AsyncOpenAI(
                api_key=llm_config["api_key"],
                base_url=llm_config["base_url"],
            )
        elif self.protocol == "anthropic":
            from anthropic import Anthropic, AsyncAnthropic
            self.client = Anthropic(api_key=llm_config["api_key"])
            self.async_client = AsyncAnthropic(api_key=llm_config["api_key"])

    def chat(self, messages: list, tools: list = None) -> dict:
        """发送对话请求"""
        if self.protocol == "openai":
            return self._chat_openai(messages, tools)
        else:
            return self._chat_anthropic(messages, tools)

    def chat_stream(self, messages: list, tools: list = None):
        """流式对话，yield StreamChunk"""
        self.last_stream_response = None
        if self.protocol == "openai":
            yield from self._stream_openai(messages, tools)
        else:
            yield from self._stream_anthropic(messages, tools)

    def _stream_openai(self, messages, tools):
        """OpenAI 流式输出"""
        kwargs = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        stream = self.client.chat.completions.create(**kwargs)
        content_buf = ""
        reasoning_buf = ""
        tool_calls_buf = {}
        
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            finish = chunk.choices[0].finish_reason

            # 思考内容
            rc = getattr(delta, "reasoning_content", None)
            if rc:
                reasoning_buf += rc
                yield StreamChunk(type="thinking", text=rc)

            # 正文内容
            if delta.content:
                content_buf += delta.content
                yield StreamChunk(type="content", text=delta.content)

            # 工具调用
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_buf:
                        tool_calls_buf[idx] = {"id": tc.id or "", "name": "", "arguments": ""}
                    if tc.id:
                        tool_calls_buf[idx]["id"] = tc.id
                    if tc.function and tc.function.name:
                        tool_calls_buf[idx]["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        tool_calls_buf[idx]["arguments"] += tc.function.arguments

            if finish in ("stop", "tool_calls"):
                break

        # 构造最终结果
        result = {
            "content": content_buf,
            "tool_calls": None,
            "stop_reason": "stop",
            "reasoning_content": reasoning_buf or None,
        }
        if tool_calls_buf:
            result["tool_calls"] = []
            result["stop_reason"] = "tool_calls"
            for idx in sorted(tool_calls_buf.keys()):
                tc = tool_calls_buf[idx]
                try:
                    args = json.loads(tc["arguments"])
                except json.JSONDecodeError:
                    args = {}
                result["tool_calls"].append({"id": tc["id"], "name": tc["name"], "arguments": args})
        self.last_stream_response = result

    def _stream_anthropic(self, messages, tools):
        """Anthropic 流式输出（简化版，非流式）"""
        response = self._chat_anthropic(messages, tools)
        if response.get("content"):
            yield StreamChunk(type="content", text=response["content"])
        self.last_stream_response = response

    def _chat_openai(self, messages, tools) -> dict:
        kwargs = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        resp = self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        result = {
            "content": msg.content or "",
            "tool_calls": None,
            "stop_reason": "stop",
            "reasoning_content": getattr(msg, "reasoning_content", None),
        }
        if msg.tool_calls:
            result["tool_calls"] = []
            for tc in msg.tool_calls:
                result["tool_calls"].append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
                })
            result["stop_reason"] = "tool_calls"
        return result

    def _chat_anthropic(self, messages, tools) -> dict:
        system_msg = ""
        api_messages = []
        for m in messages:
            if m["role"] == "system":
                system_msg = m["content"]
            else:
                api_messages.append(m)

        kwargs = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if system_msg:
            kwargs["system"] = system_msg
        if tools:
            kwargs["tools"] = self._convert_tools_to_anthropic(tools)

        resp = self.client.messages.create(**kwargs)
        result = {"content": "", "tool_calls": None, "stop_reason": "stop"}
        for block in resp.content:
            if block.type == "text":
                result["content"] += block.text
            elif block.type == "tool_use":
                if result["tool_calls"] is None:
                    result["tool_calls"] = []
                result["tool_calls"].append({
                    "id": block.id,
                    "name": block.name,
                    "arguments": block.input,
                })
                result["stop_reason"] = "tool_calls"
        return result

    def format_assistant_message(self, response: dict) -> dict:
        """格式化 assistant 消息"""
        msg = {"role": "assistant", "content": response["content"] or ""}
        if response.get("tool_calls"):
            msg["tool_calls"] = [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                for tc in response["tool_calls"]
            ]
        return msg

    def format_tool_result_message(self, tool_call_id: str, tool_name: str, result: str) -> dict:
        """格式化工具结果消息"""
        if self.protocol == "openai":
            return {"role": "tool", "tool_call_id": tool_call_id, "content": result}
        else:
            return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_call_id, "content": result}]}

    def _convert_tools_to_anthropic(self, tools: list) -> list:
        """将 OpenAI 格式工具转为 Anthropic 格式"""
        converted = []
        for t in tools:
            f = t["function"]
            converted.append({
                "name": f["name"],
                "description": f.get("description", ""),
                "input_schema": f.get("parameters", {"type": "object", "properties": {}}),
            })
        return converted
