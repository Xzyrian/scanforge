"""Export options and the batch progress window."""

from __future__ import annotations

import os
import time
from typing import Optional

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QProgressBar, QPushButton, QSpinBox, QVBoxLayout,
                               QWidget)

from .. import imageio_util as iio
from ..export import (BatchExporter, ExportSettings, ExportResult, NAME_PRESETS,
                      output_path, pdf_path, resolve_pdf_dpi)
from ..render import RESAMPLE


def _safe_zip_name(cfg) -> str:
    from ..export import _safe
    return _safe(cfg.document or "pages")


class ExportDialog(QDialog):
    def __init__(self, edits: list, cfg: ExportSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export")
        self.setMinimumWidth(560)
        self.edits = edits
        self.cfg = cfg

        root = QVBoxLayout(self)

        # ------------------------------------------------------- destination
        g_dest = QGroupBox("Destination")
        fd = QFormLayout(g_dest)

        self.ed_doc = QLineEdit(cfg.document)
        self.ed_doc.setPlaceholderText("e.g. Sony_KV-16G1")
        self.ed_doc.setToolTip("The name of this document. Every page and the "
                               "PDF are named from it.")
        fd.addRow("Document name", self.ed_doc)

        row = QHBoxLayout()
        self.ed_dir = QLineEdit(cfg.out_dir)
        b = QPushButton("Browse…")
        b.clicked.connect(self._browse)
        row.addWidget(self.ed_dir)
        row.addWidget(b)
        w = QWidget(); w.setLayout(row)
        fd.addRow("Folder", w)

        self.cmb_preset = QComboBox()
        self.cmb_preset.addItems(list(NAME_PRESETS.keys()) + ["Custom…"])
        for label, pattern in NAME_PRESETS.items():
            if pattern == cfg.naming:
                self.cmb_preset.setCurrentText(label)
                break
        else:
            self.cmb_preset.setCurrentText("Custom…")
        self.cmb_preset.currentTextChanged.connect(self._preset)
        fd.addRow("Name pages", self.cmb_preset)

        numrow = QHBoxLayout()
        self.sp_digits = QSpinBox()
        self.sp_digits.setRange(1, 8)
        self.sp_digits.setValue(cfg.digits)
        self.sp_digits.setToolTip("4 gives 0001, 0002, …")
        self.sp_start = QSpinBox()
        self.sp_start.setRange(0, 99999)
        self.sp_start.setValue(cfg.start_number)
        self.sp_start.setToolTip("The number given to the first page of each document")
        numrow.addWidget(QLabel("digits"))
        numrow.addWidget(self.sp_digits)
        numrow.addSpacing(12)
        numrow.addWidget(QLabel("first page is"))
        numrow.addWidget(self.sp_start)
        numrow.addStretch(1)
        nw = QWidget(); nw.setLayout(numrow)
        fd.addRow("Numbering", nw)

        self.ed_naming = QLineEdit(cfg.naming)
        self.ed_naming.setToolTip(
            "Tokens:  {doc} document name · {page} zero-padded page number · "
            "{name} original file name · {index} unpadded number")
        fd.addRow("Pattern", self.ed_naming)
        self.chk_subfolder = QCheckBox("Put the pages in a subfolder named after "
                                       "the document")
        self.chk_subfolder.setChecked(cfg.subfolder_per_doc)
        fd.addRow("", self.chk_subfolder)
        self.chk_overwrite = QCheckBox("Overwrite existing files")
        self.chk_overwrite.setChecked(cfg.overwrite)
        fd.addRow("", self.chk_overwrite)
        root.addWidget(g_dest)

        # ------------------------------------------------------------ format
        g_fmt = QGroupBox("Format")
        ff = QFormLayout(g_fmt)
        self.cmb_fmt = QComboBox()
        self.cmb_fmt.addItems(["TIFF", "JPEG", "PNG"])
        self.cmb_fmt.setCurrentText(cfg.fmt)
        self.cmb_fmt.currentTextChanged.connect(self._sync)
        ff.addRow("Type", self.cmb_fmt)

        self.cmb_comp = QComboBox()
        self.cmb_comp.addItems(list(iio.TIFF_COMPRESSIONS.keys()))
        self.cmb_comp.setCurrentText(cfg.compression)
        ff.addRow("TIFF compression", self.cmb_comp)

        self.sp_quality = QSpinBox()
        self.sp_quality.setRange(60, 100)
        self.sp_quality.setValue(cfg.jpeg_quality)
        ff.addRow("JPEG quality", self.sp_quality)

        self.cmb_depth = QComboBox()
        self.cmb_depth.addItems(["Same as source", "8-bit"])
        self.cmb_depth.setCurrentText(cfg.bit_depth)
        ff.addRow("Bit depth", self.cmb_depth)

        self.cmb_resample = QComboBox()
        self.cmb_resample.addItems(list(RESAMPLE.keys()))
        self.cmb_resample.setCurrentText(cfg.resample)
        ff.addRow("Resampling", self.cmb_resample)

        self.cmb_fill = QComboBox()
        self.cmb_fill.addItems(["White", "Black", "Mid grey"])
        self.cmb_fill.setCurrentIndex({255: 0, 0: 1}.get(cfg.fill, 2))
        self.cmb_fill.setToolTip("Colour of the slivers that rotation exposes at "
                                 "the corners, if your crop reaches the edge")
        ff.addRow("Rotation fill", self.cmb_fill)
        root.addWidget(g_fmt)

        # ------------------------------------------------------------- scale
        g_sc = QGroupBox("Resolution")
        fs = QFormLayout(g_sc)
        self.cmb_scale = QComboBox()
        self.cmb_scale.addItems(["Full resolution", "Target DPI", "Percent"])
        self.cmb_scale.setCurrentText(cfg.scale_mode)
        self.cmb_scale.currentTextChanged.connect(self._sync)
        fs.addRow("Mode", self.cmb_scale)
        self.sp_dpi = QDoubleSpinBox()
        self.sp_dpi.setRange(50, 1200)
        self.sp_dpi.setValue(cfg.target_dpi)
        self.sp_dpi.setSuffix(" dpi")
        fs.addRow("Target", self.sp_dpi)
        self.sp_pct = QDoubleSpinBox()
        self.sp_pct.setRange(5, 100)
        self.sp_pct.setValue(cfg.percent)
        self.sp_pct.setSuffix(" %")
        fs.addRow("Percent", self.sp_pct)
        root.addWidget(g_sc)

        # --------------------------------------------------------- extras
        g_ex = QGroupBox("Also produce")
        fe = QFormLayout(g_ex)
        self.chk_zip = QCheckBox("A zip of the exported pages, ready to send")
        self.chk_zip.setToolTip("Named <document>-images.zip. The PDF is not "
                                "included; the loose files are kept as well.")
        self.chk_zip.setChecked(cfg.zip_images)
        fe.addRow("", self.chk_zip)
        self.chk_pdf = QCheckBox("A multi-page PDF of the whole set")
        self.chk_pdf.setChecked(cfg.make_pdf)
        fe.addRow("", self.chk_pdf)
        self.sp_pdf_dpi = QDoubleSpinBox()
        self.sp_pdf_dpi.setRange(72, 1200)
        self.sp_pdf_dpi.setValue(resolve_pdf_dpi(cfg, edits))
        self.sp_pdf_dpi.setSuffix(" dpi")
        self.sp_pdf_dpi.setToolTip(
            "Defaults to the resolution of the scans themselves. A 600 dpi PDF "
            "of a long manual gets very large - drop it if you want something "
            "to email.")
        fe.addRow("PDF resolution", self.sp_pdf_dpi)
        self.chk_preview = QCheckBox("A JPEG preview beside each TIFF")
        self.chk_preview.setChecked(cfg.also_jpeg_preview)
        fe.addRow("", self.chk_preview)
        self.sp_workers = QSpinBox()
        self.sp_workers.setRange(1, 8)
        self.sp_workers.setValue(cfg.workers)
        self.sp_workers.setToolTip("Each worker holds a full page in memory; "
                                   "2-3 is the sweet spot for 600 dpi scans")
        fe.addRow("Parallel workers", self.sp_workers)
        root.addWidget(g_ex)

        self.lbl_preview = QLabel()
        self.lbl_preview.setStyleSheet(
            "color:#c8ccd3;background:#26282d;border:1px solid #3a3d44;"
            "border-radius:4px;padding:7px;font-family:Consolas,monospace;")
        self.lbl_preview.setWordWrap(True)
        self.lbl_preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.lbl_preview)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText(f"Export {len(edits)} pages")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        root.addWidget(box)

        for widget in (self.ed_dir, self.ed_naming, self.ed_doc):
            widget.textChanged.connect(self._sync)
        for sp in (self.sp_digits, self.sp_start):
            sp.valueChanged.connect(self._sync)
        for chk in (self.chk_pdf, self.chk_subfolder, self.chk_zip):
            chk.toggled.connect(self._sync)
        self._sync()

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Export to", self.ed_dir.text())
        if d:
            self.ed_dir.setText(d)

    def _preset(self, label: str) -> None:
        pattern = NAME_PRESETS.get(label)
        if pattern:
            self.ed_naming.setText(pattern)
        self.ed_naming.setEnabled(label == "Custom…" or pattern is None)
        self._sync()

    def _sync(self, *_a) -> None:
        fmt = self.cmb_fmt.currentText()
        self.cmb_comp.setEnabled(fmt == "TIFF")
        self.sp_quality.setEnabled(fmt == "JPEG" or
                                   self.cmb_comp.currentText().startswith("JPEG"))
        self.chk_preview.setEnabled(fmt == "TIFF")
        mode = self.cmb_scale.currentText()
        self.sp_dpi.setEnabled(mode == "Target DPI")
        self.sp_pct.setEnabled(mode == "Percent")
        self.sp_pdf_dpi.setEnabled(self.chk_pdf.isChecked())
        if not self.edits:
            return
        cfg = self.settings()
        try:
            lines = []
            for i in (1, 2):
                if len(self.edits) >= i:
                    lines.append(os.path.basename(output_path(self.edits[i - 1], cfg, i)))
            if len(self.edits) > 2:
                lines.append("…")
                lines.append(os.path.basename(
                    output_path(self.edits[-1], cfg, len(self.edits))))
            doc = cfg.document or ""
            folder = os.path.join(cfg.out_dir, doc) if (cfg.subfolder_per_doc and doc) \
                else cfg.out_dir
            text = f"{folder}\\\n    " + "\n    ".join(lines)
            if cfg.make_pdf:
                text += "\n" + os.path.dirname(pdf_path(cfg)) + "\\\n    " \
                        + os.path.basename(pdf_path(cfg))
            if cfg.zip_images:
                text += "\n    " + _safe_zip_name(cfg) + "-images.zip"
            sizes = {e.output_size() for e in self.edits}
            if len(sizes) == 1:
                w, h = sizes.pop()
                text += f"\n\nEvery page {w} × {h} px"
            else:
                text += f"\n\n⚠ {len(sizes)} different page sizes in this batch"
            self.lbl_preview.setText(text)
        except (KeyError, IndexError, ValueError) as exc:
            self.lbl_preview.setText(
                f"That pattern has a token I don't know: {exc}\n"
                "Use {doc}, {page}, {name} or {index}.")

    def settings(self) -> ExportSettings:
        c = self.cfg
        c.out_dir = self.ed_dir.text().strip()
        c.document = self.ed_doc.text().strip()
        c.naming = self.ed_naming.text().strip() or "{doc}_{page}"
        c.digits = self.sp_digits.value()
        c.start_number = self.sp_start.value()
        c.subfolder_per_doc = self.chk_subfolder.isChecked()
        c.overwrite = self.chk_overwrite.isChecked()
        c.fmt = self.cmb_fmt.currentText()
        c.compression = self.cmb_comp.currentText()
        c.jpeg_quality = self.sp_quality.value()
        c.bit_depth = self.cmb_depth.currentText()
        c.resample = self.cmb_resample.currentText()
        c.fill = [255, 0, 128][self.cmb_fill.currentIndex()]
        c.scale_mode = self.cmb_scale.currentText()
        c.target_dpi = self.sp_dpi.value()
        c.percent = self.sp_pct.value()
        c.zip_images = self.chk_zip.isChecked()
        c.make_pdf = self.chk_pdf.isChecked()
        c.pdf_dpi = self.sp_pdf_dpi.value()
        c.also_jpeg_preview = self.chk_preview.isChecked()
        c.workers = self.sp_workers.value()
        return c

    def accept(self) -> None:
        if not self.ed_dir.text().strip():
            self.lbl_preview.setText("Choose a destination folder first.")
            self.ed_dir.setFocus()
            return
        if "{doc}" in self.ed_naming.text() and not self.ed_doc.text().strip():
            self.lbl_preview.setText(
                "Give the document a name - every file is named from it.")
            self.ed_doc.setFocus()
            return
        super().accept()


# --------------------------------------------------------------------------
# progress
# --------------------------------------------------------------------------

class _Worker(QObject):
    progress = Signal(int, int, object)
    message = Signal(str)
    finished = Signal(list)

    def __init__(self, exporter: BatchExporter):
        super().__init__()
        self.exporter = exporter
        exporter.on_progress = lambda d, t, r: self.progress.emit(d, t, r)
        exporter.on_message = lambda m: self.message.emit(m)

    def run(self) -> None:
        results = self.exporter.run()
        self.finished.emit(results)


class ProgressDialog(QDialog):
    def __init__(self, exporter: BatchExporter, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Exporting")
        self.setMinimumWidth(620)
        self.setWindowFlag(Qt.WindowCloseButtonHint, False)
        self.exporter = exporter
        self._t0 = time.time()
        self._failures = 0

        v = QVBoxLayout(self)
        self.bar = QProgressBar()
        self.bar.setRange(0, max(1, len(exporter.edits)))
        v.addWidget(self.bar)
        self.lbl = QLabel("Starting…")
        v.addWidget(self.lbl)
        self.log = QListWidget()
        self.log.setStyleSheet("font-family:Consolas,monospace;font-size:11px;")
        v.addWidget(self.log, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_stop = QPushButton("Cancel")
        self.btn_stop.clicked.connect(self._stop)
        self.btn_close = QPushButton("Close")
        self.btn_close.setEnabled(False)
        self.btn_close.clicked.connect(self.accept)
        row.addWidget(self.btn_stop)
        row.addWidget(self.btn_close)
        v.addLayout(row)

        self.thread = QThread(self)
        self.worker = _Worker(exporter)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._progress)
        self.worker.message.connect(self._message)
        self.worker.finished.connect(self._done)
        self.thread.start()

    def _stop(self) -> None:
        self.exporter.stop()
        self.btn_stop.setEnabled(False)
        self.lbl.setText("Cancelling after the pages in flight finish…")

    def _progress(self, done: int, total: int, res: ExportResult) -> None:
        self.bar.setValue(done)
        elapsed = time.time() - self._t0
        rate = done / elapsed if elapsed else 0
        eta = (total - done) / rate if rate else 0
        self.lbl.setText(f"{done} / {total} · {rate * 60:.1f} pages/min · "
                         f"about {eta / 60:.1f} min left")
        if res.ok:
            mb = res.size_bytes / 1e6
            self.log.addItem(f"✓ {os.path.basename(res.path)}  "
                             f"{mb:.1f} MB  {res.seconds:.1f}s")
        else:
            self._failures += 1
            self.log.addItem(f"✗ {res.uid}  {res.error.splitlines()[0]}")
        self.log.scrollToBottom()

    def _message(self, msg: str) -> None:
        self.log.addItem(f"· {msg}")
        self.log.scrollToBottom()

    def _done(self, results: list) -> None:
        self.thread.quit()
        self.thread.wait(3000)
        ok = sum(1 for r in results if r.ok)
        total_mb = sum(r.size_bytes for r in results if r.ok) / 1e6
        self.lbl.setText(f"Finished · {ok} exported"
                         + (f", {self._failures} failed" if self._failures else "")
                         + f" · {total_mb:.0f} MB in "
                           f"{(time.time() - self._t0) / 60:.1f} min")
        self.btn_stop.setEnabled(False)
        self.btn_close.setEnabled(True)
        self.btn_close.setDefault(True)
        self.setWindowFlag(Qt.WindowCloseButtonHint, True)

    def closeEvent(self, ev):
        if self.thread.isRunning():
            ev.ignore()
            return
        super().closeEvent(ev)
