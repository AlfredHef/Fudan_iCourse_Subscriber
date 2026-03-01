"""LLM-based course lecture summarization via ModelScope API."""

import time

from openai import OpenAI

from . import config

SYSTEM_PROMPT = (
    "你是一个专业的课程助教。请总结以下课程录音文本的重点内容。要求：\n"
    "1. 使用Markdown格式。\n"
    "2. 语言通顺，逻辑清晰，去除口语化表达。\n"
    "3. 不要输出课程标题（标题由程序自动生成）。\n"
    '4. 直接输出总结内容，不要包含"好的"、"以下是总结"等客套话。\n'
    "5. 结构清晰，使用列表、加粗等Markdown语法。"
    "在使用markdown时，你不能使用高于三级的标题符号。"
    "也就是说，你可以用###、####但是不能用##。"
    "你需要格外注意课程中是否提及了作业、考试、签到等关键事项，如果有的话，显眼地标注在开头。\n"
    "你的总结应当尽可能详细，尽量长，包含细节，以便用户自学或复习使用。"
)


class Summarizer:
    """Course lecture summarizer using ModelScope OpenAI-compatible API."""

    def __init__(self):
        if not config.DASHSCOPE_API_KEY:
            raise ValueError("DASHSCOPE_API_KEY is not set")
        self.client = OpenAI(
            api_key=config.DASHSCOPE_API_KEY,
            base_url=config.LLM_BASE_URL,
        )
        self.models = list(config.LLM_MODELS)

    def summarize(self, title: str, content: str) -> tuple[str, str]:
        """Summarize lecture content, trying multiple models on failure.

        Args:
            title: Lecture title for context.
            content: Full transcript text.

        Returns:
            (summary, model_used) tuple.
        """
        if not content or not content.strip():
            return ("（内容为空）", "")

        errors = []
        for model in self.models:
            try:
                t0 = time.time()
                response = self.client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": f"以下是课程《{title}》的录音文本，请总结：\n\n{content}",
                        },
                    ],
                    temperature=0.3,
                    timeout=120,
                )
                result = response.choices[0].message.content
                elapsed = time.time() - t0
                print(
                    f"[Summarizer] Done ({model}): {len(content)} chars input"
                    f" → {len(result)} chars output in {elapsed:.0f}s"
                )
                return (result, model)
            except Exception as e:
                print(f"[Summarizer] {model} failed: {type(e).__name__}: {e}")
                errors.append(f"{model}: {e}")

        raise RuntimeError(
            "All LLM models failed:\n" + "\n".join(errors)
        )
