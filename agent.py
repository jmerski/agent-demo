import os
import sys
import json
import requests

API_KEY = "openrouterapikey"

# ---------------------------------------------------------
# 1. DEFINE LOCAL TOOLS (PYTHON FUNCTIONS)
# ---------------------------------------------------------
def list_files(path="."):
    """Returns a list of files in a given directory."""
    try:
        files = os.listdir(path)
        return json.dumps({"status": "success", "files": files[:20]}) # Cap at 20 files
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

# Map function names to executable Python functions
AVAILABLE_TOOLS = {
    "list_files": list_files
}

# JSON Schema declaration provided to the API endpoint
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Lists files in a given local directory path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path to inspect (defaults to '.')"
                    }
                },
                "required": []
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
        "X-Title": "Local Python Agent",
    }
    
    # State tracking: Stores system context, user intent, and tool responses
    messages = [
        {"role": "system", "content": "You are an autonomous agent with local file tools. Use available tools to fulfill user requests."},
        {"role": "user", "content": user_prompt}
    ]

    print(f"[*] Starting Agent Task: {user_prompt}\n")

    # Limit execution loops to prevent infinite runtime
    for step in range(5):
        payload = {
            "model": "openrouter/free", # Free router dynamically picks tool-calling supported models
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
        
        # Append the LLM's response to the conversation thread
        messages.append(message)

        # CHECK 1: Did the LLM request to run a tool?
        if "tool_calls" in message and message["tool_calls"]:
            tool_call = message["tool_calls"][0]
            func_name = tool_call["function"]["name"]
            func_args = json.loads(tool_call["function"]["get"] if "get" in tool_call["function"] else tool_call["function"]["arguments"])
            
            print(f"[Loop Step {step + 1}] LLM Decision: Call local function `{func_name}` with args: {func_args}")
            
            # Execute local Python function
            if func_name in AVAILABLE_TOOLS:
                tool_output = AVAILABLE_TOOLS[func_name](**func_args)
            else:
                tool_output = json.dumps({"error": "Unknown function call"})

            print(f"[*] Tool Output Executed: {tool_output}\n")

            # Feed execution result back into conversation history
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": tool_output
            })
            
            # Continue loop -> Send updated history back to LLM
            continue

        # CHECK 2: Did the LLM finish its reasoning and give a direct answer?
        if message.get("content"):
            print(f"[*] Agent Final Response:\n{message['content']}")
            break

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python agent.py \"Check what files exist in the current folder and tell me if there is a script\"")
        sys.exit(1)
        
    query = " ".join(sys.argv[1:])
    run_agent(query)
