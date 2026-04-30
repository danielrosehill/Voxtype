# Gemini 3.1 Flash-Lite Preview — API Reference

> Source: https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite-preview
> Retrieved: 2026-03-25

## Model ID

```
gemini-3.1-flash-lite-preview
```

## Description

Our most cost-efficient multimodal model, offering the fastest performance for high-frequency, lightweight tasks. Gemini 3.1 Flash-Lite is best for high-volume agentic tasks, simple data extraction, and extremely low-latency applications where budget and speed are the primary constraints.

## Specifications

| Property | Value |
|---|---|
| Model ID | `gemini-3.1-flash-lite-preview` |
| Status | Preview |
| Latest Update | March 2026 |
| Knowledge Cutoff | January 2025 |
| Input Token Limit | 1,048,576 |
| Output Token Limit | 65,536 |

## Supported Input Types

- Text
- Image
- Video
- Audio
- PDF

## Output

- Text only

## Capabilities

**Supported:**
- Batch API
- Caching
- Code execution
- File search
- Function calling
- Grounding with Google Maps
- Search grounding
- Structured outputs
- Thinking
- URL context

**Not Supported:**
- Audio generation
- Computer use
- Image generation
- Live API

---

## Use Cases & Code Examples

### Translation

Fast, cheap, high-volume translation, such as processing chat messages, reviews, and support tickets at scale.

```python
text = "Hey, are you down to grab some pizza later? I'm starving!"

response = client.models.generate_content(
    model="gemini-3.1-flash-lite-preview",
    config={
        "system_instruction": "Only output the translated text"
    },
    contents=f"Translate the following text to German: {text}"
)

print(response.text)
```

### Transcription

Process recordings, voice notes, or any audio content where you need a text transcript without spinning up a separate speech-to-text pipeline.

```python
# Upload the audio file to the GenAI File API
uploaded_file = client.files.upload(file='sample.mp3')

prompt = 'Generate a transcript of the audio.'

response = client.models.generate_content(
    model="gemini-3.1-flash-lite-preview",
    contents=[prompt, uploaded_file]
)

print(response.text)
```

### Lightweight Agentic Tasks and Data Extraction

Entity extraction, classification, and lightweight data processing pipelines supported with structured JSON output.

```python
from pydantic import BaseModel, Field

prompt = "Analyze the user review and determine the aspect, sentiment score, summary quote, and return risk"
input_text = "The boots look amazing and the leather is high quality, but they run way too small. I'm sending them back."

class ReviewAnalysis(BaseModel):
    aspect: str = Field(description="The feature mentioned (e.g., Price, Comfort, Style, Shipping)")
    summary_quote: str = Field(description="The specific phrase from the review about this aspect")
    sentiment_score: int = Field(description="1 to 5 (1=worst, 5=best)")
    is_return_risk: bool = Field(description="True if the user mentions returning the item")

response = client.models.generate_content(
    model="gemini-3.1-flash-lite-preview",
    contents=[prompt, input_text],
    config={
        "response_mime_type": "application/json",
        "response_json_schema": ReviewAnalysis.model_json_schema(),
    },
)

print(response.text)
```

### Document Processing and Summarization

Parse PDFs and return concise summaries, like for building a document processing pipeline or quickly triaging incoming files.

```python
import httpx

doc_url = "https://storage.googleapis.com/generativeai-downloads/data/med_gemini.pdf"
doc_data = httpx.get(doc_url).content

prompt = "Summarize this document"
response = client.models.generate_content(
    model="gemini-3.1-flash-lite-preview",
    contents=[
        types.Part.from_bytes(
            data=doc_data,
            mime_type='application/pdf',
        ),
        prompt
    ]
)

print(response.text)
```

### Model Routing

Use a low-latency and low-cost model as a classifier that routes queries to the appropriate model based on task complexity. This is a real pattern in production — the open-source Gemini CLI uses Flash-Lite to classify task complexity and route to Flash or Pro accordingly.

```python
FLASH_MODEL = 'flash'
PRO_MODEL = 'pro'

CLASSIFIER_SYSTEM_PROMPT = f"""
You are a specialized Task Routing AI. Your sole function is to analyze the user's request and classify its complexity. Choose between `{FLASH_MODEL}` (SIMPLE) or `{PRO_MODEL}` (COMPLEX).
1.  `{FLASH_MODEL}`: A fast, efficient model for simple, well-defined tasks.
2.  `{PRO_MODEL}`: A powerful, advanced model for complex, open-ended, or multi-step tasks.

A task is COMPLEX if it meets ONE OR MORE of the following criteria:
1.  High Operational Complexity (Est. 4+ Steps/Tool Calls)
2.  Strategic Planning and Conceptual Design
3.  High Ambiguity or Large Scope
4.  Deep Debugging and Root Cause Analysis

A task is SIMPLE if it is highly specific, bounded, and has Low Operational Complexity (Est. 1-3 tool calls).
"""

user_input = "I'm getting an error 'Cannot read property 'map' of undefined' when I click the save button. Can you fix it?"

response_schema = {
    "type": "object",
    "properties": {
        "reasoning": {
            "type": "string",
            "description": "A brief, step-by-step explanation for the model choice, referencing the rubric."
        },
        "model_choice": {
            "type": "string",
            "enum": [FLASH_MODEL, PRO_MODEL]
        }
    },
    "required": ["reasoning", "model_choice"]
}

response = client.models.generate_content(
    model="gemini-3.1-flash-lite-preview",
    contents=user_input,
    config={
        "system_instruction": CLASSIFIER_SYSTEM_PROMPT,
        "response_mime_type": "application/json",
        "response_json_schema": response_schema
    },
)

print(response.text)
```

### Thinking

For better accuracy for tasks that benefit from step-by-step reasoning, configure thinking so the model spends additional compute on internal reasoning before producing the final output.

```python
response = client.models.generate_content(
    model="gemini-3.1-flash-lite-preview",
    contents="How does AI work?",
    config=types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level="high")
    ),
)

print(response.text)
```

---

## Notes for Voxtype

- **Audio input is supported** — can send audio directly for transcription
- **Inline audio**: Use `types.Part.from_bytes(data=audio_bytes, mime_type='audio/wav')` for inline audio (no file upload needed)
- **File upload**: Use `client.files.upload()` for larger files
- **System instruction**: Pass cleanup prompt via `config={"system_instruction": prompt}`
- **This is the review/lite model** — for primary transcription, also consider `gemini-3-flash-preview` or `gemini-3.1-flash-preview`
