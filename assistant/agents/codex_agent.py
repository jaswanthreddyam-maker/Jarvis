import ollama

class CodexAgent:
    def __init__(self, model="codellama"):
        self.model = model
        self.client = ollama.AsyncClient()

    async def generate_code(self, prompt: str, context: str = "") -> str:
        system_prompt = f"You are an expert developer. Provide clean, executable python code. No explanations. Only code block.\nContext:\n{context}"
        try:
            response = await self.client.chat(model=self.model, messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ])
            return response['message']['content']
        except Exception as e:
            return str(e)
