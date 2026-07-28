import os
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import ollama
except ImportError:
    print("Install ollama first:  pip install ollama")
    sys.exit(1)

# --- Configuration ---
MODEL_NAME = "qwen2.5-coder:3b"
MAX_STEPS_PER_TURN = 30          # Increased to allow reading multiple files before moving
SUBPROC_TIMEOUT = 15
WORKSPACE_ROOT = Path.cwd().resolve()
MAX_READ_BYTES = 200_000         
BINARY_SNIFF_BYTES = 2048

# --- Path Safety ---
def safe_resolve(path_str: str) -> Path:
    if not path_str:
        path_str = "."
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = WORKSPACE_ROOT / p
    p = p.resolve()
    if p != WORKSPACE_ROOT and WORKSPACE_ROOT not in p.parents:
        raise PermissionError(f"Access denied: '{path_str}' is outside the workspace.")
    return p

def is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            chunk = f.read(BINARY_SNIFF_BYTES)
        if b"\x00" in chunk:
            return True
        text_chars = bytes(range(32, 127)) + b"\n\r\t\b\f"
        nontext = sum(1 for b in chunk if b not in text_chars)
        return len(chunk) > 0 and (nontext / len(chunk)) > 0.30
    except OSError:
        return False

# --- Strict System Prompt ---
SYSTEM_PROMPT = f"""You are a strict, literal software engineering and FILE ORGANIZATION agent.

WORKSPACE ROOT: {WORKSPACE_ROOT}
You CANNOT access paths outside this workspace.

OPERATING RULES:
1. When an action is required, emit JSON tool calls.
2. ONLY emit plain prose when the task is fully complete.
3. If you need to edit an existing file, you MUST call read_file first.
4. When using write_file, you MUST output the ENTIRE file content.
5. To ORGANIZE files by topic: 
   a. Call list_files(recursive=true).
   b. Call read_file on EVERY file to understand its contents. DO NOT guess topics based on file extensions.
   c. Determine logical topics based on what you read.
   d. Call create_folder for each topic (e.g. "Invoices", "React_Components").
   e. Call move_file to relocate each file into its correct folder.
6. You can output multiple tool calls in a single response if they do not depend on each other.

AVAILABLE TOOLS:
- list_files(path=".", recursive=false) -> str
- read_file(filename) -> str
- write_file(filename, content) -> str
- create_folder(path) -> str
- move_file(source, destination) -> str
- copy_file(source, destination) -> str
- delete_path(path) -> str
- run_script(filename) -> str

CORRECT INVOCATION EXAMPLE:
{{"name": "create_folder", "arguments": {{"path": "Reports/2024/Q1"}}}}

Begin by listing files (recursive=true) if unsure of the workspace state.
"""

# --- Tool Schema ---
TOOLS = [
    {"type": "function", "function": {
        "name": "list_files", "description": "List directory contents with type and size metadata.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "default": "."}, "recursive": {"type": "boolean", "default": False}}}
    }},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read text contents of a file. Binary files are rejected.",
        "parameters": {"type": "object", "properties": {"filename": {"type": "string"}}, "required": ["filename"]}
    }},
    {"type": "function", "function": {
        "name": "write_file", "description": "Create or overwrite a file. Auto-creates parent directories.",
        "parameters": {"type": "object", "properties": {"filename": {"type": "string"}, "content": {"type": "string"}}, "required": ["filename", "content"]}
    }},
    {"type": "function", "function": {
        "name": "create_folder", "description": "Create a directory (and missing parents). Idempotent.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    }},
    {"type": "function", "function": {
        "name": "move_file", "description": "Move or rename a file/directory.",
        "parameters": {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}}, "required": ["source", "destination"]}
    }},
    {"type": "function", "function": {
        "name": "copy_file", "description": "Copy a file to a new path.",
        "parameters": {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}}, "required": ["source", "destination"]}
    }},
    {"type": "function", "function": {
        "name": "delete_path", "description": "Delete a file or empty directory.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    }},
    {"type": "function", "function": {
        "name": "run_script", "description": "Execute a Python file.",
        "parameters": {"type": "object", "properties": {"filename": {"type": "string"}}, "required": ["filename"]
    }}
    }
]

# --- Robust Multi-JSON Fallback Parser ---
def extract_tool_calls_from_text(content: str, user_prompt: str = "") -> list:
    if not content:
        return []

    def find_json_objects(s: str):
        """Yields valid JSON strings by tracking bracket depth, ignoring strings."""
        objs = []
        start_idx = -1
        depth = 0
        in_string = False
        escape = False
        
        for i, char in enumerate(s):
            if char == '"' and not escape:
                in_string = not in_string
            elif char == '\\' and in_string:
                escape = not escape
            else:
                escape = False
            
            if char == '{' and not in_string:
                if depth == 0:
                    start_idx = i
                depth += 1
            elif char == '}' and not in_string:
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start_idx != -1:
                        objs.append(s[start_idx:i+1])
                        start_idx = -1
        return objs

    json_strings = find_json_objects(content)
    parsed_calls = []
    
    for json_str in json_strings:
        try:
            data = json.loads(json_str)
            if isinstance(data, dict) and "name" in data and "arguments" in data:
                args = data["arguments"]
                if isinstance(args, str):
                    try: 
                        args = json.loads(args)
                    except: 
                        args = {}
                # Fix syntax error was here
                if not isinstance(args, dict): 
                    args = {}
                parsed_calls.append({"function": {"name": data["name"], "arguments": args}})
        except json.JSONDecodeError:
            pass

    if parsed_calls:
        return parsed_calls

    # Fallback 2: Markdown code block interceptor
    code_block_pattern = re.compile(r"```(?:html|python|py|js|javascript|css|json|txt)?\s*\n(.*?)\n```", re.DOTALL)
    code_matches = code_block_pattern.findall(content)
    if code_matches:
        code = code_matches[0]
        fn_match = re.search(r'([a-zA-Z0-9_\-]+\.(html|py|js|css|txt|json))', user_prompt, re.IGNORECASE)
        filename = fn_match.group(1) if fn_match else "intercepted_code.html"
        print(f"[Interceptor] Caught raw code block. Saving to {filename}")
        return [{"function": {"name": "write_file", "arguments": {"filename": filename, "content": code}}}]

    # Fallback 3: Raw HTML interceptor
    stripped = content.strip()
    if stripped.startswith("<!DOCTYPE html") or stripped.startswith("<html"):
        fn_match = re.search(r'([a-zA-Z0-9_\-]+\.html)', user_prompt, re.IGNORECASE)
        filename = fn_match.group(1) if fn_match else "intercepted_code.html"
        print(f"[Interceptor] Caught raw HTML. Saving to {filename}")
        return [{"function": {"name": "write_file", "arguments": {"filename": filename, "content": stripped}}}]
    
    return []

# --- Tool Execution ---
def execute_tool(name: str, args: dict) -> str:
    try:
        if name == "list_files":
            path = safe_resolve(args.get("path", "."))
            recursive = bool(args.get("recursive", False))
            if not path.exists(): return f"ERROR: path does not exist: {path}"
            if not path.is_dir(): return f"ERROR: not a directory: {path}"
            lines = []
            if recursive:
                for root, dirs, files in os.walk(path):
                    rel_root = Path(root).relative_to(WORKSPACE_ROOT)
                    for d in sorted(dirs): lines.append(f"[DIR]  {(rel_root / d).as_posix()}/")
                    for f in sorted(files):
                        fp = Path(root) / f
                        try: size = fp.stat().st_size
                        except OSError: size = -1
                        lines.append(f"[FILE] {(rel_root / f).as_posix()}  ({size} bytes)")
            else:
                for entry in sorted(path.iterdir(), key=lambda p: p.name.lower()):
                    rel = entry.relative_to(WORKSPACE_ROOT)
                    if entry.is_dir():
                        lines.append(f"[DIR]  {rel.as_posix()}/")
                    else:
                        try: size = entry.stat().st_size
                        except OSError: size = -1
                        lines.append(f"[FILE] {rel.as_posix()}  ({size} bytes)")
            return "\n".join(lines) if lines else "(empty)"

        elif name == "read_file":
            target = safe_resolve(args["filename"])
            if not target.exists(): return f"ERROR: file not found: {target}"
            if target.is_dir(): return f"ERROR: target is a directory: {target}"
            if is_binary(target): return f"ERROR: '{target.name}' is binary. Cannot read."
            data = target.read_bytes()
            if len(data) > MAX_READ_BYTES:
                data = data[:MAX_READ_BYTES]
                note = f"\n...[truncated; file has {len(target.read_bytes())} total bytes]"
            else:
                note = ""
            return data.decode("utf-8", errors="replace") + note

        elif name == "write_file":
            target = safe_resolve(args["filename"])
            content = args["content"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} bytes to {target.relative_to(WORKSPACE_ROOT)}."

        elif name == "create_folder":
            target = safe_resolve(args["path"])
            target.mkdir(parents=True, exist_ok=True)
            return f"Folder ready: {target.relative_to(WORKSPACE_ROOT)}"

        elif name == "move_file":
            src = safe_resolve(args["source"])
            dst = safe_resolve(args["destination"])
            overwrite = bool(args.get("overwrite", False))
            if not src.exists(): return f"ERROR: source not found: {src}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                if not overwrite: return f"ERROR: destination exists: {dst}"
                if dst.is_dir(): shutil.rmtree(dst)
                else: dst.unlink()
            shutil.move(str(src), str(dst))
            return f"Moved {src.relative_to(WORKSPACE_ROOT)} -> {dst.relative_to(WORKSPACE_ROOT)}"

        elif name == "copy_file":
            src = safe_resolve(args["source"])
            dst = safe_resolve(args["destination"])
            if not src.exists(): return f"ERROR: source not found: {src}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir(): shutil.copytree(src, dst, dirs_exist_ok=True)
            else: shutil.copy2(src, dst)
            return f"Copied {src.relative_to(WORKSPACE_ROOT)} -> {dst.relative_to(WORKSPACE_ROOT)}"

        elif name == "delete_path":
            target = safe_resolve(args["path"])
            if not target.exists(): return f"ERROR: path not found: {target}"
            if target.is_dir():
                if any(target.iterdir()): return f"ERROR: directory not empty: {target}"
                target.rmdir()
            else: target.unlink()
            return f"Deleted {target.relative_to(WORKSPACE_ROOT)}"

        elif name == "run_script":
            target = safe_resolve(args["filename"])
            if not target.exists(): return f"ERROR: script not found: {target}"
            py = "python3" if shutil.which("python3") else "python"
            result = subprocess.run([py, str(target)], capture_output=True, text=True, timeout=SUBPROC_TIMEOUT, cwd=str(WORKSPACE_ROOT))
            return f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}\nexit_code: {result.returncode}"

        return f"ERROR: Unknown tool '{name}'"

    except Exception as e:
        return f"ERROR [{type(e).__name__}]: {e}"

# --- Interactive Chat Loop ---
def start_chat():
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    last_user_prompt = ""

    print(f"🤖 Agent ready. Workspace: {WORKSPACE_ROOT}")
    print("Type 'exit' to end.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if user_input.lower() in ("exit", "quit"): break
        if not user_input: continue

        last_user_prompt = user_input
        messages.append({"role": "user", "content": user_input})

        for step in range(MAX_STEPS_PER_TURN):
            print(f"\n--- STEP {step + 1} ---")

            try:
                response = ollama.chat(
                    model=MODEL_NAME, messages=messages, tools=TOOLS, stream=False,
                    options={"temperature": 0.1, "top_p": 0.85, "repeat_penalty": 1.05, "num_ctx": 8192, "num_predict": 4096}
                )
            except Exception as e:
                print(f"[Ollama error: {e}. Retrying...]")
                response = ollama.chat(model=MODEL_NAME, messages=messages, tools=TOOLS, stream=False)

            msg = response["message"]

            if not msg.get("tool_calls") and msg.get("content"):
                parsed_calls = extract_tool_calls_from_text(msg["content"], last_user_prompt)
                if parsed_calls:
                    msg["tool_calls"] = parsed_calls
                    msg["content"] = ""

            messages.append(msg)
            tool_calls = msg.get("tool_calls") or []
            content = (msg.get("content") or "").strip()

            if not tool_calls and not content:
                print("[Empty response. Forcing a tool call...]")
                messages.append({"role": "user", "content": "You output an empty response. You MUST call a tool. Start by calling list_files(recursive=true)."})
                continue

            if not tool_calls:
                print(f"\nAgent: {content}\n")
                break

            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try: args = json.loads(args)
                    except: args = {}

                display_args = args
                if name == "write_file" and "content" in args:
                    display_args = {**args, "content": "[File Content]"}
                print(f"Executing Tool: {name}  args={display_args}")

                result = execute_tool(name, args)
                preview = result if len(result) <= 300 else result[:300] + " ...[truncated]"
                print(f"Result: {preview}")
                messages.append({"role": "tool", "content": result})

            if step == MAX_STEPS_PER_TURN - 1:
                messages.append({"role": "user", "content": "Max steps reached. Stop calling tools and summarize."})

if __name__ == "__main__":
    start_chat()
