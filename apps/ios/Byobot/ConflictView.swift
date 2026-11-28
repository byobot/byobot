// A record two writers changed at once: each version side by side, and keeping one.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import SwiftUI

struct ConflictView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let conflict: Conflict

    var body: some View {
        List {
            ForEach(Array(conflict.versions.enumerated()), id: \.offset) { _, version in
                Section {
                    if case .object(let fields)? = version.payload {
                        ForEach(conflict.fields, id: \.self) { name in
                            LabeledContent(Format.words(name),
                                           value: Self.text(fields[name]))
                        }
                    } else {
                        Text("Removed it").foregroundStyle(.secondary)
                    }
                    Button("Keep this one") {
                        model.resolve(conflict, keeping: version)
                        dismiss()
                    }
                } header: {
                    Text("\(Self.author(version.actor)), \(Format.ago(version.ts))")
                }
            }
        }
        .navigationTitle(Self.title(conflict))
    }

    static func title(_ conflict: Conflict) -> String {
        conflict.name ?? Format.words(conflict.kind)
    }

    static func author(_ actor: Actor) -> String {
        switch actor.kind {
        case .human: return "You"
        case .agent: return actor.id
        default: return actor.id
        }
    }

    /// A field's value as it reads in a line: text as is, anything else as compact JSON.
    static func text(_ value: JSONValue?) -> String {
        switch value {
        case nil, .null?: return "None"
        case .string(let s)?: return s
        case .number(let n)?: return formatNumber(n)
        case .bool(let b)?: return b ? "Yes" : "No"
        case let other?:
            let data = (try? JSONEncoder().encode(other)) ?? Data()
            return String(decoding: data, as: UTF8.self)
        }
    }
}
