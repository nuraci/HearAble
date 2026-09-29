from __future__ import annotations

import ctypes as C
from dataclasses import dataclass
from pathlib import Path
import time


class BackendConfig(C.Structure):
    _fields_ = [("size", C.c_size_t), ("gpu", C.c_int32)]


class ModelConfig(C.Structure):
    _fields_ = [("size", C.c_size_t), ("path", C.c_char_p), ("name", C.c_char_p)]


class StreamingConfig(C.Structure):
    _fields_ = [
        ("size", C.c_size_t),
        ("chunk_size", C.c_float),
        ("ctc_left_padding", C.c_float),
        ("ctc_right_padding", C.c_float),
        ("rnnt_right_context", C.c_int32),
    ]


class DecoderConfig(C.Structure):
    _fields_ = [
        ("size", C.c_size_t),
        ("kind", C.c_int32),
        ("flashlight_lm", C.c_char_p),
        ("flashlight_lexicon", C.c_char_p),
        ("flashlight_tokenizer", C.c_char_p),
        ("beam_size", C.c_int32),
        ("beam_size_token", C.c_int32),
        ("beam_threshold", C.c_double),
        ("lm_weight", C.c_double),
        ("word_insertion_score", C.c_double),
        ("max_boost", C.c_double),
    ]


class EndpointingConfig(C.Structure):
    _fields_ = [
        ("size", C.c_size_t),
        ("enable", C.c_bool),
        ("vad_based", C.c_bool),
        ("stop_history_eou_ms", C.c_int32),
    ]


class RecognizerConfig(C.Structure):
    _fields_ = [
        ("size", C.c_size_t),
        ("backend", C.c_void_p),
        ("model", C.c_void_p),
        ("streaming", C.c_void_p),
        ("decoder", C.c_void_p),
        ("vad", C.c_void_p),
        ("endpointing", C.c_void_p),
        ("postproc", C.c_void_p),
        ("diar", C.c_void_p),
        ("batching", C.c_void_p),
    ]


class RecognitionOptions(C.Structure):
    _fields_ = [
        ("size", C.c_size_t),
        ("request_id", C.c_char_p),
        ("language_code", C.c_char_p),
        ("interim_results", C.c_bool),
        ("enable_word_time_offsets", C.c_bool),
        ("enable_automatic_punctuation", C.c_bool),
        ("verbatim_transcripts", C.c_bool),
        ("profanity_filter", C.c_bool),
        ("stop_history_eou_ms", C.c_int32),
        ("speech_contexts", C.c_void_p),
        ("speech_context_count", C.c_size_t),
        ("max_alternatives", C.c_int32),
        ("enable_speaker_diarization", C.c_bool),
        ("max_speaker_count", C.c_int32),
    ]


OK = 0


@dataclass
class AsrEvent:
    transcript: str
    is_final: bool
    audio_processed: float
    confidence: float


class NemoAsr:
    def __init__(
        self,
        model: Path,
        lib_dir: Path,
        language: str = "it-IT",
        gpu: int = -1,
        chunk_sec: float = 0.16,
        ctc_left_padding: float = 1.92,
        ctc_right_padding: float = 1.92,
        rnnt_right_context: int = 1,
        decoder_kind: int = 0,
        endpointing_enable: bool = False,
        stop_history_eou_ms: int = 800,
        enable_automatic_punctuation: bool = True,
        verbatim_transcripts: bool = False,
        max_alternatives: int = 1,
    ) -> None:
        self.model = model
        self.language = language.encode()
        self.enable_automatic_punctuation = enable_automatic_punctuation
        self.verbatim_transcripts = verbatim_transcripts
        self.max_alternatives = max_alternatives
        lib_path = lib_dir / "libnemo_speech_asr_c.so"
        self.lib = C.CDLL(str(lib_path))
        self._bind()

        self.backend = BackendConfig(C.sizeof(BackendConfig), gpu)
        self.model_cfg = ModelConfig(C.sizeof(ModelConfig), str(model).encode(), None)
        self.streaming = StreamingConfig(
            C.sizeof(StreamingConfig), chunk_sec, ctc_left_padding, ctc_right_padding, rnnt_right_context
        )
        self.decoder = DecoderConfig(
            C.sizeof(DecoderConfig), decoder_kind, None, None, None, 0, 0, 0.0, 0.0, 0.0, 0.0
        )
        self.endpointing = EndpointingConfig(
            C.sizeof(EndpointingConfig), endpointing_enable, False, stop_history_eou_ms
        )
        self.cfg = RecognizerConfig(
            C.sizeof(RecognizerConfig),
            C.cast(C.pointer(self.backend), C.c_void_p),
            C.cast(C.pointer(self.model_cfg), C.c_void_p),
            C.cast(C.pointer(self.streaming), C.c_void_p),
            C.cast(C.pointer(self.decoder), C.c_void_p),
            None,
            C.cast(C.pointer(self.endpointing), C.c_void_p),
            None,
            None,
            None,
        )
        self.recognizer = C.c_void_p()
        create_start = time.perf_counter()
        self._check(self.lib.nemo_speech_asr_create(C.byref(self.cfg), C.byref(self.recognizer)))
        create_seconds = time.perf_counter() - create_start
        self.stream = C.c_void_p()
        self.timings = {
            "model_load_seconds": create_seconds,
            "initialization_seconds": None,
            "create_plus_initialization_seconds": None,
        }
        self._start_stream()

    def _start_stream(self) -> None:
        opts = self.lib.nemo_speech_asr_recognition_options_default()
        opts.language_code = self.language
        opts.interim_results = True
        opts.enable_automatic_punctuation = self.enable_automatic_punctuation
        opts.verbatim_transcripts = self.verbatim_transcripts
        opts.max_alternatives = self.max_alternatives
        stream_start = time.perf_counter()
        self._check(self.lib.nemo_speech_asr_streaming_recognize(self.recognizer, C.byref(opts), C.byref(self.stream)))
        stream_seconds = time.perf_counter() - stream_start
        self.timings["initialization_seconds"] = stream_seconds
        self.timings["create_plus_initialization_seconds"] = (
            self.timings["model_load_seconds"] + stream_seconds
        )

    def reset_stream(self) -> None:
        if getattr(self, "stream", None):
            self.lib.nemo_speech_asr_stream_close(self.stream)
            self.stream = C.c_void_p()
        self._start_stream()

    def _bind(self) -> None:
        self.lib.nemo_speech_asr_recognition_options_default.restype = RecognitionOptions
        self.lib.nemo_speech_asr_create.argtypes = [C.POINTER(RecognizerConfig), C.POINTER(C.c_void_p)]
        self.lib.nemo_speech_asr_create.restype = C.c_int
        self.lib.nemo_speech_asr_destroy.argtypes = [C.c_void_p]
        self.lib.nemo_speech_asr_streaming_recognize.argtypes = [C.c_void_p, C.POINTER(RecognitionOptions), C.POINTER(C.c_void_p)]
        self.lib.nemo_speech_asr_streaming_recognize.restype = C.c_int
        self.lib.nemo_speech_asr_stream_push_f32.argtypes = [C.c_void_p, C.POINTER(C.c_float), C.c_size_t, C.c_int32]
        self.lib.nemo_speech_asr_stream_push_f32.restype = C.c_int
        self.lib.nemo_speech_asr_stream_next.argtypes = [C.c_void_p, C.POINTER(C.c_void_p)]
        self.lib.nemo_speech_asr_stream_next.restype = C.c_int
        self.lib.nemo_speech_asr_stream_finish.argtypes = [C.c_void_p]
        self.lib.nemo_speech_asr_stream_finish.restype = C.c_int
        self.lib.nemo_speech_asr_stream_close.argtypes = [C.c_void_p]
        self.lib.nemo_speech_asr_result_is_final.argtypes = [C.c_void_p]
        self.lib.nemo_speech_asr_result_is_final.restype = C.c_bool
        self.lib.nemo_speech_asr_result_audio_processed.argtypes = [C.c_void_p]
        self.lib.nemo_speech_asr_result_audio_processed.restype = C.c_float
        self.lib.nemo_speech_asr_result_transcript.argtypes = [C.c_void_p, C.c_size_t]
        self.lib.nemo_speech_asr_result_transcript.restype = C.c_char_p
        self.lib.nemo_speech_asr_result_confidence.argtypes = [C.c_void_p, C.c_size_t]
        self.lib.nemo_speech_asr_result_confidence.restype = C.c_float
        self.lib.nemo_speech_asr_result_destroy.argtypes = [C.c_void_p]
        self.lib.nemo_speech_asr_last_error.restype = C.c_char_p

    def push(self, samples) -> None:
        ptr = samples.ctypes.data_as(C.POINTER(C.c_float))
        self._check(self.lib.nemo_speech_asr_stream_push_f32(self.stream, ptr, len(samples), 16000))

    def next_events(self) -> list[AsrEvent]:
        events: list[AsrEvent] = []
        while True:
            result = C.c_void_p()
            self._check(self.lib.nemo_speech_asr_stream_next(self.stream, C.byref(result)))
            if not result:
                break
            text = self.lib.nemo_speech_asr_result_transcript(result, 0)
            events.append(
                AsrEvent(
                    transcript=(text or b"").decode("utf-8", errors="replace"),
                    is_final=bool(self.lib.nemo_speech_asr_result_is_final(result)),
                    audio_processed=float(self.lib.nemo_speech_asr_result_audio_processed(result)),
                    confidence=float(self.lib.nemo_speech_asr_result_confidence(result, 0)),
                )
            )
            self.lib.nemo_speech_asr_result_destroy(result)
        return events

    def finish(self) -> list[AsrEvent]:
        self._check(self.lib.nemo_speech_asr_stream_finish(self.stream))
        return self.next_events()

    def close(self) -> None:
        if getattr(self, "stream", None):
            self.lib.nemo_speech_asr_stream_close(self.stream)
            self.stream = None
        if getattr(self, "recognizer", None):
            self.lib.nemo_speech_asr_destroy(self.recognizer)
            self.recognizer = None

    def _check(self, status: int) -> None:
        if status != OK:
            err = self.lib.nemo_speech_asr_last_error()
            raise RuntimeError((err or b"unknown ASR error").decode("utf-8", errors="replace"))
