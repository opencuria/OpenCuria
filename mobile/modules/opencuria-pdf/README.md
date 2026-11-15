# Native PDF view

Local Expo module with the platform document view used by the file viewer (`mobile/src/tools/files/PdfPreview.tsx`). It only displays a local `file://` PDF; downloading, caching, sharing and the surrounding sheet live in JavaScript.

- iOS: PDFKit's `PDFView` — continuous pages, pinch and double-tap zoom, text selection, links and find.
- Android: the platform `PdfRenderer` in a scrolling canvas view — continuous pages, fling, pinch and double-tap zoom; the visible area is re-rendered sharply once a zoom settles.

The `OpenCuriaPdf` view takes `source` (a `file://` URI) and, on Android, `contentInsetBottom` (dp below the last page for the navigation bar). It emits `onLoad { pageCount }`, `onPageChange { page, pageCount }` and `onError { code, message }` with `code` `invalid_source`, `invalid_pdf` or `locked`.

Expo autolinks local modules from `mobile/modules/`; rebuild the development client after changes. Builds without the module fall back to sharing the PDF.
