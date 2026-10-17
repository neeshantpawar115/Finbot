"""Chat-model adapters. Anthropic is built into app.py; this module adds any OpenAI-compatible endpoint
(Google AI Studio's Gemini, Groq, OpenRouter, a local Ollama, ...) via LLM_BASE_URL, LLM_API_KEY and LLM_MODEL,
so the site can run without a Claude key. Both paths give the model the same finance tools."""
import json, os, urllib.error, urllib.request
from tools import SYSTEM, run_tool, schemas


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def post_json(url, headers, body, timeout=60):
    """POSTs JSON and returns the parsed reply, turning failures into ApiError with a helpful message."""
    req = urllib.request.Request(url, json.dumps(body).encode(), {"content-type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise ApiError(429, "The AI provider's rate limit was reached. Wait a minute and retry.")
        if e.code in (401, 403):
            raise ApiError(502, "The AI provider rejected the API key. Check LLM_API_KEY.")
        raise ApiError(502, f"The AI provider returned HTTP {e.code}.")
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise ApiError(502, "Could not reach the AI provider.")


def status():
    """Reports which chat backend is configured (never the key itself); the page uses it to pick its default provider."""
    if os.environ.get("LLM_BASE_URL"):
        return {"chat": bool(os.environ.get("LLM_MODEL")), "backend": "openai-compatible", "model": os.environ.get("LLM_MODEL", "")}
    if os.environ.get("ANTHROPIC_API_KEY"):
        return {"chat": True, "backend": "anthropic", "model": os.environ.get("FINBOT_MODEL", "claude-sonnet-5-5")}
    return {"chat": False, "backend": None, "model": ""}


def chat_openai(history, post=None):
    """Runs the tool-calling loop against an OpenAI-compatible /chat/completions endpoint."""
    post = post or post_json
    model = os.environ.get("LLM_MODEL")
    if not model:
        raise ApiError(503, "LLM_MODEL is not set on the server. Set it (and LLM_BASE_URL / LLM_API_KEY) and restart.")
    key = os.environ.get("LLM_API_KEY", "")
    url = os.environ["LLM_BASE_URL"].rstrip("/") + "/chat/completions"
    tools = [{"type": "function", "function": {"name": s["name"], "description": s["description"], "parameters": s["input_schema"]}} for s in schemas()]
    msgs, used = [{"role": "system", "content": SYSTEM}] + list(history), []
    for _ in range(6):
        m = post(url, {"Authorization": "Bearer " + key} if key else {}, {"model": model, "messages": msgs, "tools": tools})["choices"][0]["message"]
        calls = m.get("tool_calls")
        if not calls:
            return {"text": m.get("content") or "", "tools_used": used}
        msgs.append({"role": "assistant", "content": m.get("content"), "tool_calls": calls})
        for c in calls:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            used.append(c["function"]["name"])
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(run_tool(c["function"]["name"], args))})
    raise ApiError(502, "The assistant used too many tool steps. Please rephrase.")
