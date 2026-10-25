// Original Whisperstream bridge. No fluidaudio-rs implementation is included.
import Foundation
import CoreML
import FluidAudio

private final class AsrHandle {
    var manager: AsrManager?

    // Rust serializes all operations. The semaphore keeps asynchronous Swift
    // model/inference work inside the synchronous, child-owned FFI lifetime.
    func load(root: URL) -> Bool {
        let completed = DispatchSemaphore(value: 0)
        var loaded: AsrManager?
        Task {
            defer { completed.signal() }
            do {
                // Construct from exact cached files. Never call FluidAudio's
                // download/load/recovery helpers, even when a file is absent.
                let configuration = AsrModels.defaultConfiguration()
                let cpu = MLModelConfiguration()
                cpu.computeUnits = .cpuOnly
                let vocabularyData = try Data(contentsOf: root.appendingPathComponent("parakeet_v3_vocab.json"))
                guard let encoded = try JSONSerialization.jsonObject(with: vocabularyData) as? [String: String] else { return }
                var vocabulary: [Int: String] = [:]
                for (key, token) in encoded {
                    guard let index = Int(key) else { return }
                    vocabulary[index] = token
                }
                guard !vocabulary.isEmpty else { return }
                let models = AsrModels(
                    encoder: try MLModel(contentsOf: root.appendingPathComponent("Encoder.mlmodelc"), configuration: configuration),
                    preprocessor: try MLModel(contentsOf: root.appendingPathComponent("Preprocessor.mlmodelc"), configuration: cpu),
                    decoder: try MLModel(contentsOf: root.appendingPathComponent("Decoder.mlmodelc"), configuration: configuration),
                    joint: try MLModel(contentsOf: root.appendingPathComponent("JointDecisionv3.mlmodelc"), configuration: configuration),
                    configuration: configuration, vocabulary: vocabulary, version: .v3)
                let candidate = AsrManager()
                try await candidate.loadModels(models)
                // Validate decoder state before publishing readiness.
                _ = try TdtDecoderState()
                loaded = candidate
            } catch { /* Rust publishes only a fixed content-free error. */ }
        }
        completed.wait()
        manager = loaded
        return loaded != nil
    }

    func transcribe(_ samples: [Float]) -> UnsafeMutablePointer<CChar>? {
        // TRANSCRIPT DATA - DO NOT LOG.
        // Audio, token strings, native results and error details stay private
        // to this owned pipe; never print or log any of them.
        guard let manager = manager else { return nil }
        let completed = DispatchSemaphore(value: 0)
        var output: Data?
        Task {
            defer { completed.signal() }
            do {
                // Every final decode has fresh native state; preview is absent.
                var state = try TdtDecoderState()
                let result = try await manager.transcribe(samples, decoderState: &state)
                var body: [String: Any] = ["text": result.text]
                if let timings = result.tokenTimings,
                    timings.allSatisfy({ $0.startTime.isFinite && $0.endTime.isFinite
                        && $0.startTime >= 0 && $0.endTime >= $0.startTime
                        && $0.endTime <= Double(samples.count) / 16_000 }) {
                    body["tokens"] = timings.map { $0.token }
                    body["timestamps"] = timings.map { $0.startTime }
                    body["durations"] = timings.map { $0.endTime - $0.startTime }
                }
                // Confidence is not a log probability and is not transported.
                output = try JSONSerialization.data(withJSONObject: body)
            } catch { /* Transcript/audio/error content never reaches logs. */ }
        }
        completed.wait()
        guard let output = output, let encoded = String(data: output, encoding: .utf8) else { return nil }
        return strdup(encoded)
    }
}

@_cdecl("ws_asr_create")
public func wsAsrCreate() -> UnsafeMutableRawPointer {
    Unmanaged.passRetained(AsrHandle()).toOpaque()
}

@_cdecl("ws_asr_load")
public func wsAsrLoad(_ handle: UnsafeMutableRawPointer, _ root: UnsafePointer<CChar>) -> Int32 {
    let owner = Unmanaged<AsrHandle>.fromOpaque(handle).takeUnretainedValue()
    return owner.load(root: URL(fileURLWithPath: String(cString: root))) ? 1 : 0
}

@_cdecl("ws_asr_ready")
public func wsAsrReady(_ handle: UnsafeMutableRawPointer) -> Int32 {
    Unmanaged<AsrHandle>.fromOpaque(handle).takeUnretainedValue().manager == nil ? 0 : 1
}

@_cdecl("ws_asr_transcribe")
public func wsAsrTranscribe(_ handle: UnsafeMutableRawPointer, _ samples: UnsafePointer<Float>, _ count: Int) -> UnsafeMutablePointer<CChar>? {
    let owner = Unmanaged<AsrHandle>.fromOpaque(handle).takeUnretainedValue()
    return owner.transcribe(Array(UnsafeBufferPointer(start: samples, count: count)))
}

@_cdecl("ws_asr_free")
public func wsAsrFree(_ output: UnsafeMutablePointer<CChar>?) { free(output) }

@_cdecl("ws_asr_destroy")
public func wsAsrDestroy(_ handle: UnsafeMutableRawPointer) {
    Unmanaged<AsrHandle>.fromOpaque(handle).release()
}
