import ExpoModulesCore
import PDFKit
import UIKit

/// PDFKit's document view: continuous pages, pinch and double-tap zoom, text
/// selection, links and hardware-keyboard find, as in Files and Quick Look.
public final class OpenCuriaPdfView: ExpoView {
  let onLoad = EventDispatcher()
  let onError = EventDispatcher()
  let onPageChange = EventDispatcher()

  private let pdfView = PDFView()
  private var loadedSource: String?
  private var lastPage = 0

  var source: String? {
    didSet { if source != oldValue { load() } }
  }

  public required init(appContext: AppContext? = nil) {
    super.init(appContext: appContext)
    clipsToBounds = true
    pdfView.displayMode = .singlePageContinuous
    pdfView.displayDirection = .vertical
    pdfView.autoScales = true
    pdfView.pageShadowsEnabled = true
    // The React background (the app theme) surrounds the pages.
    pdfView.backgroundColor = .clear
    pdfView.isFindInteractionEnabled = true
    addSubview(pdfView)
    NotificationCenter.default.addObserver(
      self,
      selector: #selector(pageChanged),
      name: .PDFViewPageChanged,
      object: pdfView
    )
  }

  deinit {
    NotificationCenter.default.removeObserver(self)
  }

  public override func layoutSubviews() {
    super.layoutSubviews()
    guard pdfView.frame != bounds else { return }
    pdfView.frame = bounds
    pdfView.layoutIfNeeded()
    updateScaleLimits()
  }

  private func load() {
    loadedSource = source
    lastPage = 0
    pdfView.document = nil
    guard let source, !source.isEmpty else { return }
    guard let url = URL(string: source), url.isFileURL else {
      onError(["message": "Only local PDF files can be shown.", "code": "invalid_source"])
      return
    }
    DispatchQueue.global(qos: .userInitiated).async { [weak self] in
      let document = PDFDocument(url: url)
      DispatchQueue.main.async {
        guard let self, self.loadedSource == source else { return }
        self.show(document)
      }
    }
  }

  private func show(_ document: PDFDocument?) {
    guard let document else {
      onError(["message": "This PDF can't be opened.", "code": "invalid_pdf"])
      return
    }
    if document.isLocked {
      onError(["message": "This PDF is password-protected.", "code": "locked"])
      return
    }
    pdfView.document = document
    pdfView.layoutIfNeeded()
    updateScaleLimits()
    onLoad(["pageCount": document.pageCount])
    pageChanged()
  }

  /// Zoom out no further than the fitted page width, like Quick Look, and up
  /// to 5x. Assigning scaleFactor would turn autoScales off, so only clamp.
  private func updateScaleLimits() {
    let fit = pdfView.scaleFactorForSizeToFit
    guard fit > 0 else { return }
    pdfView.minScaleFactor = fit
    pdfView.maxScaleFactor = fit * 5
  }

  @objc private func pageChanged() {
    guard
      let document = pdfView.document,
      let page = pdfView.currentPage
    else { return }
    let number = document.index(for: page) + 1
    guard number != lastPage else { return }
    lastPage = number
    onPageChange(["page": number, "pageCount": document.pageCount])
  }
}
