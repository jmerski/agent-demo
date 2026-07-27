import os
import json
import re
import subprocess
from pathlib import Path
import ollama

# --- Configuration ---
MODEL_NAME = "qwen2.5-coder:3b"
MAX_STEPS_PER_TURN = 10
SUBPROC_TIMEOUT = 15

# --- Strict System Prompt ---
SYSTEM_PROMPT = """You are a collaborative autonomous software engineering agent.

OPERATING RULES (non-negotiable):
1. When an action is required, emit a single JSON tool call. No prose before it. No markdown fences.
2. Read tool results in tool_response blocks, then decide the next step.
3. Only emit plain prose when the task is fully complete or you need input from the user.
4. Never describe what you will do — just do it via a tool call.
5. You have persistent memory. You can read and modify existing files (like index.html) by calling write_file with the full, updated content.

AVAILABLE TOOLS:
- list_files(path=".") -> str : List directory contents.
- write_file(filename: str, content: str) -> str : Create or overwrite a file.
- run_script(filename: str) -> str : Execute a Python file, returning stdout, stderr, and exit code.

CORRECT INVOCATION EXAMPLE:
{"name": "list_files", "arguments": {"path": "."}}

WRONG (do not do this - do not use markdown code blocks):
{"name": "list_files", "arguments": {"path": "."}}

Begin by listing files if unsure of the workspace state."""

# --- Tool Schema ---
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in a directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_script",
            "description": "Execute a Python script in a subprocess.",
            "parameters": {
                "type": "object",
                "properties": {"filename": {"type": "string"}},
                "required": ["filename"],
            },
        },
    },
]

# --- Fallback Parser ---
TOOL_CALL_PATTERNS = [
    re.compile(r"```(?:json)?\s*\n(\{.*?\})\s*\n```", re.DOTALL),
    re.compile(r"\s*(\{.*?\})\s*", re.DOTALL),
    re.compile(r'\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{.*?\}\s*\}', re.DOTALL),
]

def extract_tool_calls_from_text(content: str) -> list:
    if not content:
        return []
    for pattern in TOOL_CALL_PATTERNS:
        matches = pattern.findall(content)
        if matches:
            for match_str in matches:
                try:
                    data = json.loads(match_str)
                    if "name" in data and "arguments" in data:
                        return [{
                            "function": {
                                "name": data["name"],
                                "arguments": data["arguments"]
                            }
                        }]
                except json.JSONDecodeError:
                    continue
    return []

# --- Tool Execution ---
def execute_tool(name: str, args: dict) -> str:
    try:
        if name == "list_files":
            path = args.get("path", ".")
            return "\n".join(sorted(os.listdir(path)))
        
        elif name == "write_file":
            filename = args["filename"]
            content = args["content"]
            Path(filename).write_text(content, encoding='utf-8')
            return f"Successfully wrote {len(content)} bytes to {filename}."
        
        elif name == "run_script":
            filename = args["filename"]
            result = subprocess.run(
                ["python", filename],  # Use 'python' for Windows compatibility
                capture_output=True,
                text=True,
                timeout=SUBPROC_TIMEOUT
            )
            return f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}\nexit_code: {result.returncode}"
            
        return f"ERROR: Unknown tool '{name}'"
    
    except subprocess.TimeoutExpired:
        return f"ERROR: Execution exceeded {SUBPROC_TIMEOUT}s timeout."
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {str(e)}"

# --- Interactive Chat Loop ---
def start_chat():
    # Persistent memory: The messages list lives here and accumulates context
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]
    
    print("🤖 Agent ready. Type 'exit' or 'quit' to end the conversation.")
    print("Type a task (e.g., 'Create a basic bootstrap index.html')\n")
    
    while True:
        user_input = input("You: ")
        if user_input.lower() in ['exit', 'quit']:
            print("Ending session.")
            break
            
        # Add user message to memory
        messages.append({"role": "user", "content": user_input})
        
        # Process agent steps for this turn
        for step in range(MAX_STEPS_PER_TURN):
            print(f"\n--- STEP {step + 1} ---")
            
            response = ollama.chat(
                model=MODEL_NAME,
                messages=messages,
                tools=TOOLS,
                stream=False,
                options={
                    "temperature": 0.1,      
                    "top_p": 0.85,
                    "repeat_penalty": 1.05,
                    "num_ctx": 8192,
                }
            )
            
            msg = response["message"]

            # Fallback parsing & state sync
            if not msg.get("tool_calls") and msg.get("content"):
                parsed_calls = extract_tool_calls_from_text(msg["content"])
                if parsed_calls:
                    print("[Fallback Parser Activated] Coerced text blob into native tool call.")
                    msg["tool_calls"] = parsed_calls
                    msg["content"] = "" 

            # Save to memory
            messages.append(msg)

            tool_calls = msg.get("tool_calls") or []
            
            # Terminal condition: Agent speaks to user without tool calls
            if not tool_calls:
                agent_reply = msg.get("content", "[No output produced]")
                print(f"\nAgent: {agent_reply}\n")
                break # End this turn, wait for next user input

            # Execute tools and feed results back into memory
            for call in tool_calls:
                fn = call["function"]
                name = fn["name"]
                args = fn["arguments"]
                
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}

                print(f"Executing Tool: {name} with args: {args if name != 'write_file' else '[File Content]'}")
                result = execute_tool(name, args)
                print(f"Result: {result[:200]}...") 
                
                # Inject result into memory
                messages.append({
                    "role": "tool",
                    "content": result
                })
            
            # If it hits the max steps, force it to speak
            if step == MAX_STEPS_PER_TURN - 1:
                print("\n[Max steps reached. Forcing agent to summarize.]")
                messages.append({"role": "user", "content": "You have reached the max step limit. Summarize the current state immediately."})

if __name__ == "__main__":
    start_chat()
