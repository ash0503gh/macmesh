import Foundation

/// Talks to the engine's control socket (~/.macmesh/control.sock): one JSON line out, one back.
struct ControlClient: Sendable {
    enum Failure: LocalizedError {
        case notRunning
        case engine(String)
        case garbled

        var errorDescription: String? {
            switch self {
            case .notRunning: "macmesh's engine isn't running on this Mac."
            case .engine(let message): message.prefix(1).uppercased() + message.dropFirst()
            case .garbled: "The engine sent a reply macmesh couldn't read."
            }
        }
    }

    var socketPath: String

    static var defaultPath: String {
        let home = ProcessInfo.processInfo.environment["MACMESH_HOME"] ?? (NSHomeDirectory() + "/.macmesh")
        return home + "/control.sock"
    }

    init(socketPath: String = ControlClient.defaultPath) {
        self.socketPath = socketPath
    }

    func call<T: Decodable>(_ method: String, _ params: [String: Any] = [:], as type: T.Type = T.self) async throws -> T {
        let request = try JSONSerialization.data(withJSONObject: ["id": 1, "method": method, "params": params])
        let reply = try await Task.detached { [socketPath] in try Self.exchange(request, path: socketPath) }.value
        guard let object = try? JSONSerialization.jsonObject(with: reply) as? [String: Any] else { throw Failure.garbled }
        if let message = object["error"] as? String { throw Failure.engine(message) }
        let result = try JSONSerialization.data(withJSONObject: object["result"] ?? NSNull(), options: [.fragmentsAllowed])
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        do { return try decoder.decode(T.self, from: result) } catch { throw Failure.garbled }
    }

    /// Blocking socket exchange; runs off the main thread.
    private static func exchange(_ request: Data, path: String) throws -> Data {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw Failure.notRunning }
        defer { close(fd) }
        var timeout = timeval(tv_sec: 30, tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8)
        guard bytes.count < MemoryLayout.size(ofValue: address.sun_path) else { throw Failure.notRunning }
        withUnsafeMutableBytes(of: &address.sun_path) { buffer in
            buffer.copyBytes(from: bytes)
            buffer[bytes.count] = 0
        }
        let connected = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        guard connected == 0 else { throw Failure.notRunning }
        var line = request
        line.append(0x0A)
        let sent = line.withUnsafeBytes { send(fd, $0.baseAddress, $0.count, 0) }
        guard sent == line.count else { throw Failure.notRunning }
        var reply = Data()
        var chunk = [UInt8](repeating: 0, count: 65536)
        while reply.last != 0x0A {
            let n = recv(fd, &chunk, chunk.count, 0)
            if n <= 0 { break }
            reply.append(contentsOf: chunk[0 ..< n])
        }
        guard !reply.isEmpty else { throw Failure.notRunning }
        return reply
    }
}
