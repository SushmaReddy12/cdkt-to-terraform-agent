# Developer Tips for `cdkt`

This document provides quick tips for extending the `cdkt` tool as a developer.

## 1. How to use LiteLLM to support OpenAI (GPT-4o)

`cdkt` natively uses the Anthropic Python SDK (`client.messages.create`). If you want to use OpenAI models without rewriting the core `agent.py` tool-calling loops, the easiest path is to use **LiteLLM**. It acts as a drop-in proxy that seamlessly translates Anthropic API calls into OpenAI payloads.

### Setup
1. Install LiteLLM:
   ```bash
   pip install litellm
   ```
2. Run the LiteLLM proxy locally with your OpenAI key:
   ```bash
   export OPENAI_API_KEY="sk-your-openai-key"
   litellm --model gpt-4o
   ```
   *This starts a proxy server at `http://0.0.0.0:4000`.*
3. Configure `cdkt` to route through the proxy.
   Update your `.env` file to point the base URL to your local proxy. (The proxy requires a dummy Anthropic key).
   ```env
   ANTHROPIC_API_KEY=dummy-key
   CDKT_BASE_URL=http://0.0.0.0:4000
   CDKT_MODEL=gpt-4o
   ```

With this configuration, the Anthropic SDK in `cdkt` will talk to your local LiteLLM proxy, which will handle the OpenAI translation automatically.

---

## 2. How to add new AWS Infrastructure sources (e.g., SAM, Serverless Framework)

`cdkt/cli.py` uses a highly scalable **Plugin Strategy Pattern**. Adding support for a completely new framework (like AWS SAM or Amplify) is completely isolated from the existing CDK logic.

### Step-by-step:
1. **Create your Pipeline File:**
   Create a new file (e.g., `cdkt/sam_pipeline.py`). It must expose a `convert` function with the same signature as `cdkt/pipeline.py`. 
   *(Goal: Your pipeline just needs to write raw Terraform `.tf` files to the provided `out_dir` sandbox).*
   
2. **Register the Pipeline:**
   Open `cdkt/cli.py` and locate the `PIPELINES` registry. Add your new framework along with a simple lambda function that deterministically detects it (e.g., looking for `samconfig.toml`).
   
   ```python
           PIPELINES = {
               "CDK": {
                   "detect": lambda d: (d / "cdk.json").exists(),
                   "module": "cdkt.pipeline"
               },
               "CFN": {
                   "detect": lambda d: (d / "template.yaml").exists() or (d / "template.json").exists(),
                   "module": "cdkt.cfn_pipeline"
               },
               # Add your new framework here:
               "SAM": {
                   "detect": lambda d: (d / "samconfig.toml").exists(),
                   "module": "cdkt.sam_pipeline" 
               }
           }
   ```

3. **Enjoy the Shared Integrator!**
   That is all you have to do to wire it up! You don't need to touch the AI intent prompts (the router automatically injects your new key into the AI's supported list). 
   
   Best of all, because your custom pipeline outputs standard Terraform to the sandbox, it automatically inherits the core **Integrator Agent** and the `terraform validate` security checks before it ever touches the user's disk.
