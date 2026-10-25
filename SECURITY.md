# Security

Report a security issue privately to [support@whisperstream.io](mailto:support@whisperstream.io). Include the source revision, Windows version, reproduction steps and expected behavior. Use synthetic recordings and text. Never send API keys, passwords or personal transcripts. No response deadline is promised.

The app handles audio and text locally. Optional enhancement providers receive content only through their configured, consented workflows. Development must use an isolated `WHISPERSTREAM_DATA_DIR`. Keep the backend and Vite server on loopback; do not expose them through a public proxy.

For security changes, run affected regression checks, dependency audits and secret scans. Audit findings describe affected dependencies, not necessarily reachable app vulnerabilities. Record what was tested and any native or packaged verification limits in the review.

When built from this source, the app makes no application-update requests. Update it by rebuilding from newer source. When installed from the official installer on the website, the app checks for signed updates and installs one only after you accept the prompt. You can also check manually from About.
