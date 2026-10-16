"""Invoke a real framework tool locally; no Agent/Runner, provider or API key."""

import argparse
import asyncio
import json

from supplier_payment_assistant import SupplierPaymentTools


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--framework", choices=("langchain", "openai"), required=True)
    args = parser.parse_args()
    tools = SupplierPaymentTools()
    if args.framework == "langchain":
        result = await tools.langchain_tools()[0].ainvoke({})
    else:
        from agents.tool_context import ToolContext

        context = ToolContext(
            context=None, tool_name="offline_demo", tool_call_id="local-demo",
            tool_arguments="{}",
        )
        result = await tools.openai_tools()[0].on_invoke_tool(context, "{}")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
