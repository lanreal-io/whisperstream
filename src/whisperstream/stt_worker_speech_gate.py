"""Child-only Silero runtime over the existing framed worker transport."""
import os
from .speech_gate import SpeechGate, SpeechGateSettings
from .stt_worker_protocol import (PROTOCOL_VERSION, SttWorkerProtocolMismatch,
                                 SttWorkerValidationError, decode_audio_request)


class SpeechGateWorkerRuntime:
    engine_id = 'speech_gate'
    capabilities = {}

    def __init__(self):
        self._gate = None

    def dispatch(self, request, payload):
        if request.backend != self.engine_id:
            raise SttWorkerValidationError('worker backend is unavailable')
        operation = request.operation
        if operation == 'hello':
            if PROTOCOL_VERSION not in request.body.get('supported_versions', []):
                raise SttWorkerProtocolMismatch('no mutually supported protocol version')
            return {'selected_version': PROTOCOL_VERSION, 'worker_pid': os.getpid(),
                    'engine_id': self.engine_id, 'capabilities': {},
                    'limits': {'audio_dtype': 'float32-le', 'audio_rank': 1}}, False
        if operation == 'load':
            self._gate = SpeechGate(SpeechGateSettings(**request.body))
            return {'state': 'ready', 'loaded': True}, False
        if operation == 'admit':
            waveform, rate = decode_audio_request(request, payload)
            if self._gate is None:
                raise SttWorkerValidationError('speech detector is not loaded')
            admitted = self._gate.has_speech(waveform, sample_rate=rate)
            return {'admitted': admitted, 'detector_failed': self._gate.failed}, False
        if operation in {'unload', 'shutdown'}:
            if self._gate is not None:
                self._gate.close()
            self._gate = None
            return {'state': 'idle', 'loaded': False}, operation == 'shutdown'
        raise SttWorkerValidationError('unsupported worker operation')
