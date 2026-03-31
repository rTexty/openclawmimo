#!/usr/bin/env python3
import argparse
import sys
import json
from pathlib import Path

# Add project root to sys.path so we can import from lenochka-memory
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lenochka-memory"))

try:
    import mem
except ImportError as e:
    print(f"Error importing mem.py: {e}")
    sys.exit(1)


def cmd_store(args):
    """Обертка над mem.store_memory и mem.chaos_store"""
    contact_id = args.contact_id
    chat_thread_id = args.chat_thread_id
    message_id = args.message_id
    importance = args.importance
    label = args.label
    text = args.text

    try:
        # store_memory expects standard kwargs
        mem.store(
            content=text,
            mem_type='episodic',
            importance=importance,
            contact_id=contact_id,
            chat_thread_id=chat_thread_id,
            source_message_id=message_id,
            content_hash=None, # will be computed automatically or handled by caller
            auto_associate=False # Skip heavy LLM association in sync adapter
        )
        
        # chaos_store 
        mem.chaos_store(
            content=text[:200],
            category=label,
            priority=importance,
            contact_id=contact_id
        )
        print(json.dumps({"status": "success", "message": f"Successfully stored {label} with importance {importance}"}))
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)


def cmd_recall(args):
    """Обертка над mem.recall"""
    query = args.query
    contact_id = args.contact_id
    limit = args.limit
    
    try:
        results = mem.recall(query=query, limit=limit, include_chaos=True)
        # mem.recall currently prints to stdout and returns nothing or returns formatted string
        # Assuming it returns a string based on mem.py structure or prints it out.
        # We will capture it or just let mem.recall print it.
        # Let's ensure we return JSON for OpenClaw to easily parse if needed, or just text.
        print("\n--- RECALL RESULTS ---")
        if results:
            print(results)
        else:
            print("No relevant memories found.")
    except Exception as e:
        print(f"Error during recall: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Lenochka Memory Manager Adapter for OpenClaw")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Store command
    store_p = subparsers.add_parser("store", help="Store a memory fact/decision/risk")
    store_p.add_argument("--text", type=str, required=True, help="Text to store")
    store_p.add_argument("--importance", type=float, default=0.6, help="Importance (0.0 - 1.0)")
    store_p.add_argument("--label", type=str, required=True, help="Category label (task, decision, risk, etc)")
    store_p.add_argument("--contact_id", type=int, default=None, help="CRM Contact ID")
    store_p.add_argument("--chat_thread_id", type=int, default=None, help="Chat Thread ID")
    store_p.add_argument("--message_id", type=int, default=None, help="Source Message ID for dedup")

    # Recall command
    recall_p = subparsers.add_parser("recall", help="Recall memories based on query")
    recall_p.add_argument("--query", type=str, required=True, help="Search query")
    recall_p.add_argument("--contact_id", type=int, default=None, help="CRM Contact ID for filtering")
    recall_p.add_argument("--limit", type=int, default=5, help="Number of results")

    args = parser.parse_args()

    if args.command == "store":
        cmd_store(args)
    elif args.command == "recall":
        cmd_recall(args)

if __name__ == "__main__":
    main()
