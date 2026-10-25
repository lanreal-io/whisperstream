// Caret placement for HUD text fields. A programmatic focus leaves the caret
// at the start of the text, so the user's next keys would land before what is
// already there. Native activation also moves keyboard focus off the WebView
// and back, which can reset the caret mid-typing.

export type FieldCarets = WeakMap<HTMLElement, { start: number; end: number }>;

/** Focus a field with its caret after the text. A focused field is left alone. */
export function focusFieldAtEnd(field: HTMLTextAreaElement | HTMLInputElement | null): void {
  if (field === null || document.activeElement === field) return;
  field.focus({ preventScroll: true });
  const length = field.value.length;
  field.setSelectionRange(length, length);
}

/** Remember the caret the user placed, from input, key, pointer and select events. */
export function rememberFieldCaret(event: Event, carets: FieldCarets): void {
  const field = event.target;
  if (!(field instanceof HTMLTextAreaElement) && !(field instanceof HTMLInputElement)) return;
  if (field.selectionStart === null || field.selectionEnd === null) return;
  carets.set(field, { start: field.selectionStart, end: field.selectionEnd });
}

/** Put a field back in focus with the caret the user last placed in it. */
export function restoreFieldCaret(field: Element | null, carets: FieldCarets): void {
  if (!(field instanceof HTMLTextAreaElement) && !(field instanceof HTMLInputElement)) return;
  if (!field.isConnected) return;
  const caret = carets.get(field);
  const refocused = document.activeElement !== field;
  if (refocused) field.focus({ preventScroll: true });
  const length = field.value.length;
  if (caret !== undefined) {
    field.setSelectionRange(Math.min(caret.start, length), Math.min(caret.end, length));
  } else if (refocused) {
    field.setSelectionRange(length, length);
  }
}
