


export type ErrorCode =
  | "no_input_device"
  | "backend_disconnect"
  | "model_load_failed"
  | "stt_engine_failed"
  | "audio_capture_failed";


export type RetryAction =
  | "rescan_devices"
  | "recheck_health"
  | "reload_model"
  | "restart_capture";


/** Payload emitted on the `"popup-error:data"` Tauri event. The popup
 *  listens for this to hydrate its render without a second round-trip. */
export interface ErrorEmitPayload {
  code: ErrorCode;
  title: string;
  message: string;
  retryAction?: RetryAction;
}
