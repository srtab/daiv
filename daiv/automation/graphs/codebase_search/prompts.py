grade_system = """You are a grader assessing relevance of a retrieved snippet of code to a query and its intent.
It does not need to be a stringent test. The goal is to filter out erroneous/irrelevant retrievals.
"""
grade_human = "Query: {query}\nIntent of the query: {query_intent}\n\nRetrieved snippet:\n{document}"


re_write_system = """Act as a search query rewriter to improve the relevance and precision of code search queries.
The rewritten queries should include more **code-related keywords**.
Focus on keywords that developers would typically use when searching for code snippets.

## Tips
1. Use synonyms of the keywords to increase the chances of finding the relevant code snippet.
2. Avoid ambiguous terms in the query to get precise results.
3. Don't use: "code", "snippet", "example", "sample", etc. as they are redundant.

## Examples:
Query: class FOOField
Improved query: class implementation FOOField

Query: get all elements from a list
Improved query: retrieve all elements from a list

Query: sort a list of integers
Improved query: order a list of integers
"""

re_write_human = "Initial query: {query}\nIntent of the query: {query_intent}\nFormulate an improved query."
