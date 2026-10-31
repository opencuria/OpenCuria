// Adapted from t3code (https://github.com/pingdotgg/t3code), MIT License,
// Copyright (c) 2026 T3 Tools Inc. See LICENSE and THIRD_PARTY_NOTICES.md.

package expo.modules.opencuriaterminal

import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition

class OpenCuriaTerminalModule : Module() {
  override fun definition() = ModuleDefinition {
    Name("OpenCuriaTerminalSurface")

    // Bumped when native hardware-keyboard handling changes; surfaced in the JS debug
    // logs so a stale native binary is distinguishable from a broken key pipeline.
    Constants(
      "hardwareKeyRevision" to 2,
      "outputRevision" to 1,
    )

    View(OpenCuriaTerminalView::class) {
      AsyncFunction("writeOutput") { view: OpenCuriaTerminalView, data: String, reset: Boolean ->
        view.writeOutput(data, reset)
      }

      Prop("terminalKey") { view: OpenCuriaTerminalView, terminalKey: String ->
        view.terminalKey = terminalKey
      }

      Prop("initialBuffer") { view: OpenCuriaTerminalView, initialBuffer: String ->
        view.initialBuffer = initialBuffer
      }

      Prop("fontSize") { view: OpenCuriaTerminalView, fontSize: Double ->
        view.fontSize = fontSize.toFloat()
      }

      Prop("focusRequest") { view: OpenCuriaTerminalView, focusRequest: Double ->
        view.focusRequest = focusRequest
      }

      Prop("readOnly") { view: OpenCuriaTerminalView, readOnly: Boolean ->
        view.readOnly = readOnly
      }

      Prop("autoFocus") { view: OpenCuriaTerminalView, autoFocus: Boolean ->
        view.autoFocus = autoFocus
      }

      Prop("appearanceScheme") { view: OpenCuriaTerminalView, appearanceScheme: String ->
        view.appearanceScheme = appearanceScheme
      }

      Prop("themeConfig") { view: OpenCuriaTerminalView, themeConfig: String ->
        view.themeConfig = themeConfig
      }

      Prop("backgroundColor") { view: OpenCuriaTerminalView, backgroundColor: String ->
        view.backgroundColorHex = backgroundColor
      }

      Prop("foregroundColor") { view: OpenCuriaTerminalView, foregroundColor: String ->
        view.foregroundColorHex = foregroundColor
      }

      Prop("mutedForegroundColor") { view: OpenCuriaTerminalView, mutedForegroundColor: String ->
        view.mutedForegroundColorHex = mutedForegroundColor
      }

      Prop("captureRequest") { view: OpenCuriaTerminalView, request: Double ->
        view.captureRequest = request
      }

      Prop("sharedGrid") { view: OpenCuriaTerminalView, sharedGrid: List<Int> ->
        view.sharedGrid = sharedGrid
      }
      Events("onInput", "onResize", "onCapture")

      OnViewDidUpdateProps { view: OpenCuriaTerminalView ->
        view.applyPendingBuffer()
      }

      OnViewDestroys { view: OpenCuriaTerminalView ->
        view.cleanup()
      }
    }
  }
}
