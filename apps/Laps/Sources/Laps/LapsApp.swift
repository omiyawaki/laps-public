import SwiftUI

@main
struct LapsApp: App {
    @StateObject private var store = RunStore()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(store)
                .frame(minWidth: 920, minHeight: 620)
        }
        .windowStyle(.titleBar)
    }
}
