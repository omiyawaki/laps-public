import Foundation

final class GitRunLogSync: @unchecked Sendable {
    static let live = GitRunLogSync()

    private let gitExecutable: String
    private let environment: [String: String]
    private let queue = DispatchQueue(label: "local.osamu.laps.git-run-log-sync")

    init(
        gitExecutable: String = "/usr/bin/git",
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) {
        self.gitExecutable = gitExecutable
        self.environment = environment
    }

    func sync(fileURL: URL, reason: String) {
        let disabled = environment["LAPS_DISABLE_GIT_SYNC"] == "1"
            || environment["LAPS_DISABLE_GIT_SYNC"] == "true"
        guard !disabled else {
            return
        }

        let fileURL = fileURL.standardizedFileURL
        let gitExecutable = gitExecutable
        let environment = environment
        queue.async {
            do {
                try Self.syncRunLog(
                    fileURL: fileURL,
                    reason: reason,
                    gitExecutable: gitExecutable,
                    environment: environment
                )
            } catch {
                fputs("Laps git sync skipped: \(error)\n", stderr)
            }
        }
    }

    private static func syncRunLog(
        fileURL: URL,
        reason: String,
        gitExecutable: String,
        environment: [String: String]
    ) throws {
        guard FileManager.default.isExecutableFile(atPath: gitExecutable) else {
            throw GitRunLogSyncError.missingGit(gitExecutable)
        }
        guard let repoRoot = findRepositoryRoot(containing: fileURL),
              let relativePath = relativePath(for: fileURL, in: repoRoot) else {
            return
        }

        let status = try runGit(
            ["status", "--porcelain", "--", relativePath],
            repoRoot: repoRoot,
            gitExecutable: gitExecutable,
            environment: environment
        )
        guard !status.stdout.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return
        }

        _ = try runGit(
            ["add", "--", relativePath],
            repoRoot: repoRoot,
            gitExecutable: gitExecutable,
            environment: environment
        )
        let stagedDiff = try runGit(
            ["diff", "--cached", "--quiet", "--", relativePath],
            repoRoot: repoRoot,
            gitExecutable: gitExecutable,
            environment: environment,
            allowedExitCodes: [0, 1]
        )
        guard stagedDiff.exitCode == 1 else {
            return
        }

        _ = try runGit(
            ["commit", "-m", commitMessage(for: reason), "--", relativePath],
            repoRoot: repoRoot,
            gitExecutable: gitExecutable,
            environment: environment
        )
        try pushIfConfigured(
            repoRoot: repoRoot,
            gitExecutable: gitExecutable,
            environment: environment
        )
    }

    private static func pushIfConfigured(
        repoRoot: URL,
        gitExecutable: String,
        environment: [String: String]
    ) throws {
        let upstream = try runGit(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            repoRoot: repoRoot,
            gitExecutable: gitExecutable,
            environment: environment,
            allowedExitCodes: [0, 128]
        )
        if upstream.exitCode == 0 {
            _ = try runGit(
                ["push"],
                repoRoot: repoRoot,
                gitExecutable: gitExecutable,
                environment: environment
            )
            return
        }

        let remotes = try runGit(
            ["remote"],
            repoRoot: repoRoot,
            gitExecutable: gitExecutable,
            environment: environment
        )
        let hasOrigin = remotes.stdout
            .split(whereSeparator: \.isNewline)
            .contains("origin")
        guard hasOrigin else {
            return
        }

        _ = try runGit(
            ["push", "-u", "origin", "HEAD"],
            repoRoot: repoRoot,
            gitExecutable: gitExecutable,
            environment: environment
        )
    }

    private static func findRepositoryRoot(containing fileURL: URL) -> URL? {
        var directory = fileURL.deletingLastPathComponent().standardizedFileURL
        while true {
            let gitPath = directory.appendingPathComponent(".git").path
            if FileManager.default.fileExists(atPath: gitPath) {
                return directory
            }
            let parent = directory.deletingLastPathComponent().standardizedFileURL
            if parent.path == directory.path {
                return nil
            }
            directory = parent
        }
    }

    private static func relativePath(for fileURL: URL, in repoRoot: URL) -> String? {
        let filePath = fileURL.standardizedFileURL.path
        let rootPath = repoRoot.standardizedFileURL.path
        let prefix = rootPath + "/"
        guard filePath.hasPrefix(prefix) else {
            return nil
        }
        return String(filePath.dropFirst(prefix.count))
    }

    private static func commitMessage(for reason: String) -> String {
        let trimmedReason = reason.trimmingCharacters(in: .whitespacesAndNewlines)
        let title = trimmedReason.isEmpty ? "Update run log data" : trimmedReason
        return "\(title)\n\nAutomatically committed by Laps after updating the project run log."
    }

    private static func runGit(
        _ arguments: [String],
        repoRoot: URL,
        gitExecutable: String,
        environment: [String: String],
        allowedExitCodes: Set<Int32> = [0]
    ) throws -> GitCommandResult {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: gitExecutable)
        process.currentDirectoryURL = repoRoot
        process.arguments = arguments

        var processEnvironment = environment
        processEnvironment["GIT_TERMINAL_PROMPT"] = "0"
        process.environment = processEnvironment

        let output = Pipe()
        let error = Pipe()
        process.standardOutput = output
        process.standardError = error

        try process.run()
        process.waitUntilExit()

        let result = GitCommandResult(
            exitCode: process.terminationStatus,
            stdout: String(data: output.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? "",
            stderr: String(data: error.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        )
        guard allowedExitCodes.contains(result.exitCode) else {
            throw GitRunLogSyncError.commandFailed(
                arguments: arguments,
                exitCode: result.exitCode,
                stderr: result.stderr
            )
        }
        return result
    }
}

private struct GitCommandResult {
    let exitCode: Int32
    let stdout: String
    let stderr: String
}

private enum GitRunLogSyncError: Error, CustomStringConvertible {
    case missingGit(String)
    case commandFailed(arguments: [String], exitCode: Int32, stderr: String)

    var description: String {
        switch self {
        case .missingGit(let path):
            return "git executable not found at \(path)"
        case .commandFailed(let arguments, let exitCode, let stderr):
            let command = (["git"] + arguments).joined(separator: " ")
            let detail = stderr.trimmingCharacters(in: .whitespacesAndNewlines)
            if detail.isEmpty {
                return "\(command) failed with exit code \(exitCode)"
            }
            return "\(command) failed with exit code \(exitCode): \(detail)"
        }
    }
}
