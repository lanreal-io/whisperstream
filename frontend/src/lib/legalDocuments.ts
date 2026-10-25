import privacyPdf from "../assets/legal/PRIVACY.pdf?url";
import eulaPdf from "../assets/legal/EULA.pdf?url";
import { saveBlob } from "./saveBlob";

export const LEGAL_DOCUMENTS = [
  { label: "Privacy policy (PDF)", url: privacyPdf, filename: "Whisperstream-Privacy.pdf" },
  { label: "License agreement (PDF)", url: eulaPdf, filename: "Whisperstream-License.pdf" },
];

export async function downloadLegalDocument(document: typeof LEGAL_DOCUMENTS[number]) {
  const response = await fetch(document.url);
  if (!response.ok) throw new Error("Bundled document unavailable");
  return saveBlob(await response.blob(), document.filename, "pdf");
}
