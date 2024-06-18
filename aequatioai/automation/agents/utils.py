import backoff
import openai
from litellm import completion


@backoff.on_exception(backoff.expo, openai.RateLimitError)
def completion_with_retries(*args, **kwargs):
    return completion(*args, **kwargs)
