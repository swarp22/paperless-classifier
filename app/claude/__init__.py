"""Claude API Client für PDF-Klassifizierung.

Stellt den Claude-Client, Response-Modelle, Prompt-Builder und
Kosten-Tracking bereit.

Typische Verwendung:
    from app.claude import ClaudeClient, PromptData, build_system_prompt

    data = PromptData(correspondents=[...], document_types=[...], ...)
    system_prompt = build_system_prompt(data)

    async with ClaudeClient(api_key="sk-ant-...") as client:
        response = await client.classify_document(pdf_bytes, system_prompt)
        print(response.result.title, response.usage.cost_usd)
"""

from app.claude.client import (
    ClassificationResponse,
    ClassificationResult,
    ClaudeAPIError,
    ClaudeClient,
    ClaudeConfigError,
    ClaudeError,
    ClaudeResponseError,
    ConfidenceLevel,
    CostLimitReachedError,
    CreateNew,
    LinkExtraction,
    TextMessageResponse,
)
from app.claude.cost_tracker import (
    CostTracker,
    ModelPricing,
    PRICING_TABLE,
    TokenUsage,
    calculate_cost,
)
from app.claude.prompts import (
    PromptData,
    build_schema_rules_text,
    build_system_prompt,
    build_user_prompt,
)

__all__ = [
    # Client
    "ClaudeClient",
    "ClassificationResponse",
    "ClassificationResult",
    "TextMessageResponse",
    "ConfidenceLevel",
    "LinkExtraction",
    "CreateNew",
    # Exceptions
    "ClaudeError",
    "ClaudeConfigError",
    "ClaudeAPIError",
    "ClaudeResponseError",
    "CostLimitReachedError",
    # Kosten
    "CostTracker",
    "TokenUsage",
    "ModelPricing",
    "PRICING_TABLE",
    "calculate_cost",
    # Prompts
    "PromptData",
    "build_system_prompt",
    "build_user_prompt",
    "build_schema_rules_text",
]
