# Local, Ollama, and cloud enhancement providers

Whisperstream 2.0.0.

## Choose where cleanup runs

AI Enhancement optionally cleans a completed dictation before Whisperstream inserts it. Choose **Local** to run cleanup on this computer, or configure Ollama, Gemini, OpenAI, Anthropic, or a Custom service. Enhancement is off by default.

| Provider | Where cleanup runs | What you configure |
| --- | --- | --- |
| Local | On this computer | Stream-1 Mini (default) or Customizable (Gemma 4 E2B), with a model-specific speed check |
| Ollama | Where the selected Ollama model runs: this computer, another computer, or Ollama Cloud | **Endpoint** and exact **Model** name |
| Gemini | Google Gemini | **API key** and **Model** |
| OpenAI | OpenAI | **API key** and **Model** |
| Anthropic | Anthropic | **API key** and **Model** |
| Custom | The OpenAI-compatible service at your **Base URL** | **Base URL**, **Model**, and **API key** |

## Enable AI Enhancement

1. Open **Enhancement**.
2. Select a **Provider**.
3. Review any provider notices that appear. Continue only if you want to use that provider.
4. Complete the fields or guided Local setup shown for the provider.
5. Turn on **AI Enhancement** if it is still off.

For a configured service, wait for the status to show **Connected**. Local setup is complete when its download and speed check finish successfully. If setup fails, expand the connection card and follow the message shown.

The provider controls where cleanup runs. To choose how text is rewritten, see [Profiles and routing](enhancement-profiles-routing.md).

## Choose a cloud model

For Gemini, OpenAI, or Anthropic, open **Model** to choose from a curated list. The **Recommended** label identifies a suggested starting point for that provider.

Whisperstream uses the list bundled with the app and makes no model-list requests. A new release can update the list without changing your saved model IDs or selections. To use a model that is not listed, choose **Custom model name…** and enter its exact identifier. Your provider account must have access to that model.

Cloud providers still need an internet connection and their own account or API access. The bundled model list does not make cloud cleanup available offline.

## If cleanup times out or fails

Whisperstream delivers Original text if a provider times out or returns an error. In a long dictation, affected parts use Original while successful parts remain enhanced.

The HUD then briefly names the provider and shows **timed out** or **returned an error**.

## Adjust Thinking and output safeguards

Open **Enhancement** and expand **Advanced**. These settings apply to the provider and model you selected.

**Thinking** lets a supported model spend extra time before answering. **Off** is the default where the model allows it. Models that require thinking start at their lowest supported setting. When the control is unavailable, the provider's default applies, or the model keeps its fixed settings. Your choice is remembered for that model and connection across all profiles. Higher levels may take longer, and changing Thinking does not extend **Enhancement timeout**. Whisperstream inserts only the model's final text, without its reasoning.

**Output safeguards** is on by default. For Local, it repairs clear digit errors and uses the original transcript when the output fails its checks. For other providers, it uses the original transcript if the model refuses, repeats itself in a loop, or returns a runaway response. Turn it off to use the model's response without these checks. Empty responses and failed requests still fall back to the original transcript. Safeguards are basic checks: they cannot catch every word a model changes or adds.

## Set up Local enhancement

Choose **Local** when cleanup must remain on this computer. Stream-1 Mini is the default: Whisperstream's own cleanup model for English Chat and Email, with built-in prompts. Choose Customizable (Gemma 4 E2B) in the model picker when you want to write your own prompts. Customizable automatically uses the verified official Google Gemma backup if its primary download source is unavailable. A manually restored, verified Google Gemma artifact is also supported. The two Gemma artifacts may produce different results; the speed check measures the one in use. Local cleanup does not need an API key. Code profiles produce original text with Stream-1 Mini. Local enhancement works the same way on Windows and Mac.

1. Select **Local** under **Provider**.
2. The Local setup guide checks Stream-1 Mini. An existing file is reused; a missing one is downloaded first. Choose **Start speed check**. To use Customizable instead, choose it in the model picker; Whisperstream then checks only that model.
3. Follow the result shown to finish setup.
4. Turn on **AI Enhancement** if it is still off.

After setup, cleanup runs on this computer. Each model needs its own speed check. If one model cannot be downloaded, you can check and use the other independently. See [Install and recover models offline](model-recovery.md) for exact files, manual installation, and single-model setup.

## Set up Gemini

Gemini is the recommended quick setup for a bring-your-own-key cloud provider.

1. Open Google's [Gemini API key guide](https://ai.google.dev/gemini-api/docs/api-key) and sign in to Google AI Studio.
2. Create or select a Google Cloud project if prompted.
3. Create an API key and copy it. Follow Google's current restriction guidance on the same page.
4. In Whisperstream, select **Gemini** and paste the key into **API key**.
5. Keep the recommended **Model** or choose another available to your key, then complete the standard enablement steps above.

Google may offer a free tier for eligible accounts and models. Regional availability, limits, data terms, and pricing can change, so check the current [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing).

## Configure another provider

### Ollama

Follow the official [Ollama quickstart](https://docs.ollama.com/quickstart), then enter the server address in **Endpoint** and the exact model name in **Model**. Ollama supports [local and cloud models](https://docs.ollama.com/cloud), so confirm where the selected model runs before enabling enhancement.

### OpenAI or Anthropic

Create a provider key, paste it into **API key**, and choose a **Model**. Use the official [OpenAI API quickstart](https://developers.openai.com/api/docs/quickstart) or [Anthropic API keys](https://platform.claude.com/settings/keys) page for current account steps. Provider terms and billing apply.

For Anthropic, the **Workspace ID** field appears only after a connection check reports a workspace problem. Copy the value from Anthropic Console and try again, or use a workspace-scoped API key that does not need this field. The field stays editable while you remain on the page with the same provider and key. Replacing or removing the Anthropic key also clears the saved workspace.

### Custom

Select **Custom** for a service that accepts OpenAI-compatible requests. Enter its **Base URL**, exact **Model** identifier, and **API key**, then enable enhancement. Obtain those values from the service's documentation.
