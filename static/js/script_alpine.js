document.addEventListener('alpine:init', () => {
    Alpine.data('ocrApp', () => ({
      /* ───────────── STATE ───────────── */
      pages: [],
      currentViewedPageIndex: null,
      selectedPagesIndices: [],
  
      imageZoomLevel: 1,
      isPanning: false,
      panStartX: 0,
      panStartY: 0,
      imgOffsetX: 0,
      imgOffsetY: 0,
  
      ocrLanguage: 'vie',
      isUploading: false,
      isGeneratingMergedPdf: false,
      isGeneratingSinglePdf: false,
  
      currentViewedPageOcrText: '',
      ocrTextChangedManually: false,
      globalMessage: { text: '', type: 'info' },
      globalMessageTimeout: null,
  
      /* polling */
      masterPollingInterval: null,
      activeTaskIds: new Set(),
      activePdfTask: { id: null, status: '', message: '' },
  
      /* ───────── GETTERS ───────── */
      get currentViewedPage() {
        return this.currentViewedPageIndex !== null && this.pages[this.currentViewedPageIndex]
          ? this.pages[this.currentViewedPageIndex]
          : null;
      },
      get selectablePageIndices() {
        return this.pages
          .map((p, i) => (!p.status || (p.status !== 'processing_ocr' && p.status !== 'processing_upload')) ? i : null)
          .filter(i => i !== null);
      },
      get areAllSelectablePagesChecked() {
        return this.selectablePageIndices.length > 0 &&
               this.selectablePageIndices.every(i => this.selectedPagesIndices.includes(i));
      },
      isProcessingAnyPage() {
        return this.pages.some(p => p.status === 'processing_ocr' || p.status === 'processing_upload');
      },
  
      /* ───────── LIFECYCLE ───────── */
      init() {
        this.$watch('currentViewedPageIndex', () => {
          if (this.currentViewedPage) {
            this.currentViewedPageOcrText = this.currentViewedPage.ocrText;
            this.ocrTextChangedManually = false;
            this.resetZoom();
          } else {
            this.currentViewedPageOcrText = '';
          }
        });
        window.addEventListener('beforeunload', () => {
          if (this.masterPollingInterval) clearInterval(this.masterPollingInterval);
          this.pages.forEach(p => {
            if (p.thumbnailUrl?.startsWith('blob:')) URL.revokeObjectURL(p.thumbnailUrl);
            if (p.imageUrl?.startsWith('blob:')) URL.revokeObjectURL(p.imageUrl);
          });
        });
      },
  
      /* ───────── UI helpers ───────── */
      showGlobalMessage(text, type = 'info', duration = 4000) {
        if (this.globalMessageTimeout) clearTimeout(this.globalMessageTimeout);
        this.globalMessage = { text, type };
        if (duration) this.globalMessageTimeout = setTimeout(() => this.globalMessage.text = '', duration);
      },
  
      /* ───────── PAN / ZOOM ───────── */
      zoomImage(delta) {
        this.imageZoomLevel = Math.max(0.1, Math.min(5, this.imageZoomLevel + delta));
        this.applyTransform();
      },
      resetZoom() {
        this.imageZoomLevel = 1;
        this.imgOffsetX = this.imgOffsetY = 0;
        this.applyTransform();
      },
      applyTransform() {
        const img = document.getElementById('displayedImage');
        if (img) img.style.transform = `translate(${this.imgOffsetX}px, ${this.imgOffsetY}px) scale(${this.imageZoomLevel})`;
      },
      startPan(e) {
        const img = document.getElementById('displayedImage');
        if (!img || e.button !== 0) return;
        this.isPanning = true;
        this.panStartX = e.clientX - this.imgOffsetX;
        this.panStartY = e.clientY - this.imgOffsetY;
        img.style.cursor = 'grabbing';
      },
      panImage(e) {
        if (!this.isPanning) return;
        e.preventDefault();
        this.imgOffsetX = e.clientX - this.panStartX;
        this.imgOffsetY = e.clientY - this.panStartY;
        this.applyTransform();
      },
      endPan() {
        if (!this.isPanning) return;
        this.isPanning = false;
        const img = document.getElementById('displayedImage');
        if (img) img.style.cursor = 'grab';
      },
  
      /* ──────── SELECT ALL ──────── */
      toggleSelectAllPages() {
        const selectable = this.selectablePageIndices;
        if (!selectable.length) return;
        if (this.areAllSelectablePagesChecked) {
          this.selectedPagesIndices = this.selectedPagesIndices.filter(i => !selectable.includes(i));
        } else {
          this.selectedPagesIndices = Array.from(new Set([...this.selectedPagesIndices, ...selectable]));
        }
      },
      areAllPagesSelected() { return this.areAllSelectablePagesChecked; },
  
      /* ──────────── UPLOAD & PAGE LIST ──────────── */
      async addFilesToPages(files) {
        if (!files?.length) return;
        this.isUploading = true;
        const form = new FormData();
        Array.from(files).forEach(f => f.type.startsWith('image/') && form.append('files', f));
        if (form.getAll('files').length === 0) {
          this.isUploading = false;
          this.showGlobalMessage('Không có file ảnh hợp lệ.', 'info');
          return;
        }
        try {
          const res = await fetch('/upload/', { method: 'POST', body: form });
          if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
          const { uploaded_files } = await res.json();
          uploaded_files.forEach(sf => {
            const cf = Array.from(files).find(f => f.name === sf.original_filename);
            if (!cf) return;
            this.pages.push({
              id: sf.temp_filename,
              originalFilename: sf.original_filename,
              tempFilename: sf.temp_filename,
              thumbnailUrl: URL.createObjectURL(cf),
              imageUrl: URL.createObjectURL(cf),
              ocrText: '',
              originalOcrText: '',
              status: 'pending_ocr',
              statusText: 'Sẵn sàng OCR',
              langUsedForOcr: ''
            });
          });
          if (this.pages.length && this.currentViewedPageIndex === null) this.currentViewedPageIndex = 0;
          this.showGlobalMessage(`${uploaded_files.length} trang đã tải lên.`, 'success');
        } catch (e) {
          this.showGlobalMessage(`Lỗi tải lên: ${e.message}`, 'error');
        } finally {
          this.isUploading = false;
        }
      },
      handleBookUpload(e) {
        if (e.target.files.length) {
          this.pages = [];
          this.currentViewedPageIndex = null;
          this.selectedPagesIndices = [];
          this.addFilesToPages(e.target.files);
        }
        e.target.value = null;
      },
      addPagesFromInput(e) { this.addFilesToPages(e.target.files); e.target.value = null; },
      selectPage(i) {
        const p = this.pages[i];
        if (!p || p.status === 'processing_ocr' || p.status === 'processing_upload') return;
        this.currentViewedPageIndex = i;
      },
      toggleSelectAll() { this.toggleSelectAllPages(); },
  
      /* ──────────── OCR MULTI TASK ──────────── */
      async ocrSelectedOrPendingPages(onlySelected = false) {
        const pagesToRun = (onlySelected ? this.selectedPagesIndices.map(i => this.pages[i]) : this.pages)
          .filter(p => p?.tempFilename && (p.status === 'pending_ocr' || p.status === 'error_ocr'));
        if (!pagesToRun.length) {
          this.showGlobalMessage(onlySelected ? 'Các trang đã chọn không cần OCR.' : 'Không có trang nào cần OCR.', 'info');
          return;
        }
        pagesToRun.forEach(p => { p.status = 'processing_ocr'; p.statusText = 'Đang gửi…'; });
        try {
          const res = await fetch('/ocr-multiple-pages/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pages_to_ocr: pagesToRun.map(p => ({ temp_filename: p.tempFilename, original_filename: p.originalFilename })), lang: this.ocrLanguage })
          });
          if (!res.ok) throw new Error(res.statusText);
          const { submitted_tasks } = await res.json();
          submitted_tasks.forEach(t => {
            const p = this.pages.find(pg => pg.tempFilename === t.temp_filename);
            if (p && t.task_id) {
              p.statusText = 'Đang đợi xử lý…';
              this.activeTaskIds.add(t.task_id);
            }
          });
          this.startMasterPolling();
          this.showGlobalMessage('Đã gửi yêu cầu OCR.', 'info');
        } catch (e) {
          this.showGlobalMessage(`Lỗi gửi OCR: ${e.message}`, 'error');
          pagesToRun.forEach(p => { p.status = 'pending_ocr'; p.statusText = 'Sẵn sàng OCR'; });
        }
      },
  
      /* ──────────── TASK POLLING ──────────── */
      startMasterPolling() {
        if (this.masterPollingInterval || (!this.activeTaskIds.size && !this.activePdfTask.id)) return;
        this.masterPollingInterval = setInterval(() => {
          const tasks = [...this.activeTaskIds];
          if (this.activePdfTask.id) tasks.push(this.activePdfTask.id);
          if (!tasks.length) return;
          tasks.forEach(id => this.checkSingleTask(id));
        }, 5000);
      },
      async checkSingleTask(taskId) {
        try {
          const r = await fetch(`/task-status/${taskId}`);
          if (!r.ok) return console.warn('HTTP error', r.status);
          const s = await r.json();
          if (!['SUCCESS', 'FAILURE'].includes(s.status)) return;
          const result = s.result || {};
          let done = false;
          if (result.merged_pdf_filename) { this.handlePdfTaskCompletion(s); done = true; }
          else if (result.temp_filename)    { done = this.handleOcrTaskCompletion(s); }
          else console.warn('Unknown result struct');
          if (done || s.status === 'FAILURE') {
            this.activeTaskIds.delete(taskId);
            if (this.activePdfTask.id === taskId) this.activePdfTask.id = null;
          }
        } catch (e) { console.error('poll error', e); this.activeTaskIds.delete(taskId); }
      },
      handleOcrTaskCompletion(s) {
        const r = s.result || {}, p = this.pages.find(pg => pg.tempFilename === r.temp_filename);
        if (!p) return true;
        const txt = r.text || r.ocr_text || '';
        if (s.status === 'SUCCESS' && r.status === 'success' && txt.trim()) {
          p.ocrText = p.originalOcrText = txt;
          p.status = 'ocr_done';
          p.statusText = 'Đã OCR xong';
          p.langUsedForOcr = r.lang_used || r.lang || this.ocrLanguage;
          if (this.currentViewedPage?.id === p.id) this.currentViewedPageOcrText = txt;
        } else {
          p.status = 'error_ocr';
          p.statusText = `Lỗi: ${r.error?.slice(0, 80) || 'Không rõ'}`;
        }
        return true;
      },
      handlePdfTaskCompletion(s) {
        this.isGeneratingMergedPdf = false;
        const r = s.result;
        if (s.status === 'SUCCESS') {
          this.showGlobalMessage('Tạo PDF thành công!', 'success');
          this.downloadBlobFromUrl(`/download-generated-pdf/${r.merged_pdf_filename}`, r.merged_pdf_filename);
        } else {
          this.showGlobalMessage(`Tạo PDF thất bại: ${r.error || 'Không rõ'}`, 'error');
        }
        this.activePdfTask = { id: null, status: '', message: '' };
      },
  
      /* ──────────── PDF MERGE ──────────── */
      async downloadMergedSearchablePdf() {
        if (this.activePdfTask.id) return this.showGlobalMessage('Đang có tác vụ PDF khác.', 'info');
        const pages = this.pages.filter(p => p.tempFilename);
        if (!pages.length) return this.showGlobalMessage('Không có trang để tạo PDF.', 'info');
        if (this.isProcessingAnyPage()) return this.showGlobalMessage('Hãy đợi tất cả trang OCR xong.', 'info');
        this.isGeneratingMergedPdf = true;
        try {
          const res = await fetch('/generate-book-pdf-async/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ files_to_process: pages.map(p => ({ temp_filename: p.tempFilename, original_filename: p.originalFilename, lang: p.langUsedForOcr || this.ocrLanguage })) })
          });
          if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
          const { task_id } = await res.json();
          this.activePdfTask = { id: task_id, status: 'SUBMITTED', message: '' };
          this.startMasterPolling();
        } catch (e) {
          this.showGlobalMessage(`Lỗi gửi task PDF: ${e.message}`, 'error');
          this.isGeneratingMergedPdf = false;
        }
      },
  
      /* ──────────── DOWNLOAD UTILS ──────────── */
      downloadBlob(blob, name) {
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.hidden = true;
        a.href = url;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        URL.revokeObjectURL(url);
        a.remove();
      },
      async downloadBlobFromUrl(url, name) {
        try {
          const r = await fetch(url);
          if (!r.ok) throw new Error(r.statusText);
          this.downloadBlob(await r.blob(), name);
        } catch (e) { this.showGlobalMessage(`Lỗi tải file: ${e.message}`, 'error'); }
      }
  
    }));
  });