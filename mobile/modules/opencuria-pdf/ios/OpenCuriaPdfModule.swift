import ExpoModulesCore

public class OpenCuriaPdfModule: Module {
  public func definition() -> ModuleDefinition {
    Name("OpenCuriaPdf")

    View(OpenCuriaPdfView.self) {
      Events("onLoad", "onError", "onPageChange")

      Prop("source") { (view: OpenCuriaPdfView, source: String?) in
        view.source = source
      }
    }
  }
}
