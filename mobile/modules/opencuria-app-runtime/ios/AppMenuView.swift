// Copyright OpenCuria contributors. AGPL-3.0-only.

import SwiftUI
import UIKit

/**
 The app menu: a sheet like Expo Go's developer menu, reduced to the app,
 Close app and Reload, and switches for the ways that open the menu.
 */
struct AppMenuView: View {
  let name: String
  let icon: UIImage?
  @ObservedObject var triggers: AppMenuTriggers
  let onDismiss: () -> Void
  let onCloseApp: () -> Void
  let onReload: () -> Void

  var body: some View {
    NavigationStack {
      Form {
        Section {
          Button(action: onCloseApp) {
            Label("Close app", systemImage: "rectangle.portrait.and.arrow.right")
          }
          Button(action: onReload) {
            Label("Reload", systemImage: "arrow.clockwise")
          }
        }
        Section("Open this menu with") {
          toggle(.shake, "Shake")
          toggle(.threeFingerPress, "Three-finger long press")
          toggle(.button, "Menu button")
        }
      }
      .navigationBarTitleDisplayMode(.inline)
      .toolbar {
        ToolbarItem(placement: .principal) {
          HStack(spacing: 8) {
            if let icon {
              Image(uiImage: icon)
                .resizable()
                .frame(width: 24, height: 24)
                .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                .accessibilityHidden(true)
            }
            Text(name)
              .font(.headline)
              .lineLimit(1)
          }
        }
        ToolbarItem(placement: closePlacement) {
          closeButton
        }
      }
    }
  }

  /// The last way that is on stays on: its switch is disabled.
  private func toggle(_ trigger: AppMenuTrigger, _ title: String) -> some View {
    Toggle(title, isOn: Binding(
      get: { triggers.isEnabled(trigger) },
      set: { triggers.set(trigger, on: $0) }
    ))
    .disabled(triggers.isLast(trigger))
  }

  private var closePlacement: ToolbarItemPlacement {
    if #available(iOS 17.0, *) {
      return .topBarTrailing
    }
    return .navigationBarTrailing
  }

  /// The system close button, as on OpenCuria's settings sheet.
  @ViewBuilder private var closeButton: some View {
    if #available(iOS 26.0, *) {
      Button(role: .close, action: onDismiss)
    } else {
      Button(action: onDismiss) {
        Image(systemName: "xmark")
      }
      .accessibilityLabel("Close")
    }
  }
}
