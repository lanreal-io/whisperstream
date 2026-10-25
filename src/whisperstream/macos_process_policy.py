"""Keep the Darwin backend out of the Dock and the app switcher."""

from __future__ import annotations

import ctypes
import ctypes.util
import logging

# NSApplicationActivationPolicyAccessory: no Dock icon or menu bar, the same
# policy an LSUIElement agent gets.
_ACCESSORY_POLICY = 1


def make_backend_accessory(logger: logging.Logger) -> bool:
    """Mark this process as an accessory app before AppKit is used elsewhere.

    The source backend runs inside Python.app and the packaged sidecar is a
    plain executable, so the first AppKit or window-server call otherwise
    registers a regular app: a Dock icon whose Quit stops the backend.
    """
    try:
        appkit_path = ctypes.util.find_library("AppKit")
        objc_path = ctypes.util.find_library("objc")
        if not (appkit_path and objc_path):
            raise OSError("framework unavailable")
        ctypes.CDLL(appkit_path)
        objc = ctypes.CDLL(objc_path)
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        send_id = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
            ("objc_msgSend", objc)
        )
        send_policy = ctypes.CFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long
        )(("objc_msgSend", objc))
        application = send_id(
            objc.objc_getClass(b"NSApplication"),
            objc.sel_registerName(b"sharedApplication"),
        )
        if not application:
            raise OSError("application unavailable")
        applied = bool(
            send_policy(
                application,
                objc.sel_registerName(b"setActivationPolicy:"),
                _ACCESSORY_POLICY,
            )
        )
    except (OSError, AttributeError, ValueError) as exc:
        logger.warning(
            "macos backend activation policy unavailable error_type=%s",
            type(exc).__name__,
        )
        return False
    logger.info("macos backend activation policy accessory applied=%s", applied)
    return applied
