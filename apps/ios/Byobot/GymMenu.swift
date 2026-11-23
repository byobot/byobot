// Pick the gym you're at, or add one. Loads compare within a gym.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import SwiftUI

struct GymMenu: View {
    @EnvironmentObject var model: AppModel
    @State private var adding = false
    @State private var name = ""

    var body: some View {
        let current = model.book.currentLocationId
        Menu {
            Picker("Gym", selection: Binding(get: { current }, set: { model.setLocation($0) })) {
                ForEach(model.book.locations) { Text($0.name).tag(String?.some($0.id)) }
                Text("Not set").tag(String?.none)
            }
            Button("Add a gym…", systemImage: "plus") {
                name = ""
                adding = true
            }
        } label: {
            Label(GymMenu.name(model.book.location(current), id: current), systemImage: "mappin.and.ellipse")
        }
        .alert("Add a gym", isPresented: $adding) {
            TextField("Name", text: $name)
            Button("Add") {
                let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
                if !trimmed.isEmpty { model.addLocation(name: trimmed) }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Loads you lift there compare only with each other.")
        }
    }

    static func name(_ location: Location?, id: String?) -> String {
        location?.name ?? id ?? "Gym not set"
    }
}
