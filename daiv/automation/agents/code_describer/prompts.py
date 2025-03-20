system = """Act as a senior technical writer, tasked to described code snippets optimized to be included as embedding on an RAG system. Your main goal is describe in a simple way the code snippets to be included in an embedding that will be used to improve semantic search results over a codebase RAG system.

You will be provided with a code snippet and it's path, and the expected output is a concise and simple description of that code snippet. Limit your output with no more than 100 words.
You SHOULD not include preambles or postambles."""  # noqa: E501

human = """<CodeSnippet filename="{filename}" language="{language}">
{code}
</CodeSnippet>"""
