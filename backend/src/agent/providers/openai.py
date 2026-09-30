from openai import OpenAI


def complete(
    client: OpenAI, model: str, messages: list[dict], tools: list[dict]
) -> dict:
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools,
    )
    choice = response.choices[0]
    if choice.finish_reason not in {"stop", "tool_calls"}:
        raise RuntimeError(f"模型未正常完成：{choice.finish_reason}")
    message = choice.message
    result = {"role": "assistant", "content": message.content}
    if message.tool_calls:
        result["tool_calls"] = [call.model_dump() for call in message.tool_calls]
    return result
