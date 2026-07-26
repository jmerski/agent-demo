import os
import sys
import json
import requests
import subprocess

API_KEY = "openrouterapikey"

# ---------------------------------------------------------
# 1. DEFINE LOCAL TOOLS
# ---------------------------------------------------------
def list_files(path="."):
    """Lists files in the target directory."""
    try:
        files = os.listdir(path)
        return json.dumps({"status": "success", "files": files})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

def write_file(filename, content):
    """Writes or overwrites a text file locally."""
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        return json.dumps({"status": "success", "message": f"Successfully created {filename}"})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

def run_script(filename):
    """Executes a local Python script and captures its stdout/stderr."""
    try:
        # Run process with a 15-second timeout to prevent infinite loops
        result = subprocess.run(
            [sys.executable, filename],
            capture_output=True,
            text=True,
            timeout=15
        )
        return json.dumps({
            "status": "success",
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr
        })
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

AVAILABLE_TOOLS = {
    "list_files": list_files,
    "write_file": write_file,
    "run_script": run_script
}

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Lists files in a given local directory path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path (defaults to '.')"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Creates or updates a text file in the local directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Target filename (e.g. test.py)"},
                    "content": {"type": "string", "description": "Complete source code/text to write"}
                },
                "required": ["filename", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_script",
            "description": "Runs a Python script in a subprocess and captures terminal output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Script name to run (e.g. test.py)"}
                },
                "required": ["filename"]
            }
        }
    }
]

# ---------------------------------------------------------
# 2. AGENT EXECUTION LOOP
# ---------------------------------------------------------
def run_agent(user_prompt):
    url = "https://openrouter.ai/api/v1/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost",
        "X-Title": "Local Developer Agent",
    }
    
    messages = [
        {
            "role": "system", 
            "content": "You are a software engineer agent. You can write files and execute scripts. If a script fails or outputs errors, analyze the output, edit the file, and re-run it until it works."
        },
        {"role": "user", "content": user_prompt}
    ]

    print(f"[*] Task: {user_prompt}\n")

    for step in range(10): # Up to 10 loops to allow write -> run -> debug cycles
        payload = {
            "model": "openrouter/free",
            "messages": messages,
            "tools": TOOLS_SCHEMA,
            "tool_choice": "auto"
        }

        response = requests.post(url, headers=headers, json=payload)
        
        if response.status_code != 200:
            print(f"[!] API Error [{response.status_code}]: {response.text}")
            break

        res_json = response.json()
        message = res_json["choices"][0]["message"]
        messages.append(message)

        if "tool_calls" in message and message["tool_calls"]:
            tool_call = message["tool_calls"][0]
            func_name = tool_call["function"]["name"]
            
            # Handle arguments parsing robustly
            raw_args = tool_call["function"].get("arguments", "{}")
            func_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            
            print(f"[Step {step + 1}] Calling `{func_name}` with: {func_args}")
            
            if func_name in AVAILABLE_TOOLS:
                tool_output = AVAILABLE_TOOLS[func_name](**func_args)
            else:
                tool_output = json.dumps({"error": "Tool not found"})

            print(f"[*] Result: {tool_output}\n")

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": tool_output
            })
            continue

        if message.get("content"):
            print(f"[*] Final Output:\n{message['content']}")
            break

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python builder_agent.py \"Write a python script called check_system.py that prints system info and run it\"")
        sys.exit(1)
        
    query = " ".join(sys.argv[1:])
    run_agent(query)
