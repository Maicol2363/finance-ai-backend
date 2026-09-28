import os

# Must be set before any application modules are imported during pytest collection
os.environ["FINANCE_AI_ENVIRONMENT"] = "test"
os.environ.pop("FINANCE_AI_LLM_PROVIDER", None)
