(() => {
  "use strict";

  const form = document.getElementById("compliance-document-upload");
  const statusNode = document.getElementById("compliance-upload-status");
  if (!form || !statusNode) return;

  const isFrench = (document.documentElement.lang || "en").toLowerCase().startsWith("fr");
  const MAX_FILE_BYTES = 12 * 1024 * 1024;
  const MAX_TEXT_CHARS = 120000;
  const DB_NAME = "wai-intelligence-local-documents";
  const DB_VERSION = 1;
  const STORE_NAME = "documents";
  const scriptPromises = new Map();
  let pdfModulePromise = null;

  const messages = {
    chooseFile: ["Choose a document first.", "Sélectionnez d’abord un document."],
    tooLarge: ["The document exceeds the 12 MB limit.", "Le document dépasse la limite de 12 Mo."],
    unsupported: ["Choose a PDF, DOCX, TXT or Markdown file.", "Choisissez un fichier PDF, DOCX, TXT ou Markdown."],
    parsing: ["Reading the document in this browser…", "Lecture du document dans ce navigateur…"],
    saving: ["Saving the original in this browser and sending extracted text to Wai…", "Enregistrement de l’original dans ce navigateur et envoi du texte extrait à Wai…"],
    success: ["Document saved. Opening the compliance register…", "Document enregistré. Ouverture du registre de conformité…"],
    noText: ["No selectable text was found. The original is saved in this browser; scanned PDFs may require OCR before review.", "Aucun texte sélectionnable n’a été trouvé. L’original est enregistré dans ce navigateur; les PDF numérisés peuvent nécessiter une reconnaissance optique avant examen."],
    missingLocal: ["This original file is not available in this browser. It was saved in the browser used for the upload.", "Ce fichier original n’est pas disponible dans ce navigateur. Il a été enregistré dans le navigateur utilisé lors du téléversement."],
    downloadFailed: ["The original document could not be retrieved from this browser.", "Impossible de récupérer le document original dans ce navigateur."],
    genericError: ["The document could not be saved. Please try again.", "Le document n’a pas pu être enregistré. Veuillez réessayer."],
    libraryError: ["A document-reading library could not be loaded. Check the browser connection and try again.", "Une bibliothèque de lecture de documents n’a pas pu être chargée. Vérifiez la connexion du navigateur et réessayez."],
    noIdb: ["This browser does not provide IndexedDB storage, so the original cannot be retained safely.", "Ce navigateur ne fournit pas le stockage IndexedDB; l’original ne peut donc pas être conservé de façon fiable."],
    uploadFailed: ["Wai could not save the document record.", "Wai n’a pas pu enregistrer la fiche du document."]
  };

  const message = (key) => messages[key][isFrench ? 1 : 0];

  function setStatus(text, isError = false) {
    statusNode.textContent = text;
    statusNode.classList.toggle("text-error", Boolean(isError));
    statusNode.setAttribute("role", isError ? "alert" : "status");
  }

  function loadScript(url, globalName) {
    if (window[globalName]) return Promise.resolve(window[globalName]);
    if (scriptPromises.has(url)) return scriptPromises.get(url);

    const promise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = url;
      script.async = true;
      script.crossOrigin = "anonymous";
      script.onload = () => window[globalName]
        ? resolve(window[globalName])
        : reject(new Error(`Library loaded without ${globalName}`));
      script.onerror = () => reject(new Error(`Could not load ${url}`));
      document.head.appendChild(script);
    });

    scriptPromises.set(url, promise);
    return promise;
  }

  function openDocumentDb() {
    if (!("indexedDB" in window)) return Promise.reject(new Error("IndexedDB is unavailable"));

    return new Promise((resolve, reject) => {
      const request = window.indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(STORE_NAME)) {
          db.createObjectStore(STORE_NAME, { keyPath: "id" });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error("Could not open IndexedDB"));
      request.onblocked = () => reject(new Error("IndexedDB upgrade is blocked by another tab"));
    });
  }

  async function idbWrite(record) {
    const db = await openDocumentDb();
    try {
      await new Promise((resolve, reject) => {
        const transaction = db.transaction(STORE_NAME, "readwrite");
        transaction.objectStore(STORE_NAME).put(record);
        transaction.oncomplete = () => resolve();
        transaction.onerror = () => reject(transaction.error || new Error("IndexedDB write failed"));
        transaction.onabort = () => reject(transaction.error || new Error("IndexedDB write aborted"));
      });
    } finally {
      db.close();
    }
  }

  async function idbRead(id) {
    const db = await openDocumentDb();
    try {
      return await new Promise((resolve, reject) => {
        const transaction = db.transaction(STORE_NAME, "readonly");
        const request = transaction.objectStore(STORE_NAME).get(id);
        request.onsuccess = () => resolve(request.result || null);
        request.onerror = () => reject(request.error || new Error("IndexedDB read failed"));
      });
    } finally {
      db.close();
    }
  }

  async function idbDelete(id) {
    const db = await openDocumentDb();
    try {
      await new Promise((resolve, reject) => {
        const transaction = db.transaction(STORE_NAME, "readwrite");
        transaction.objectStore(STORE_NAME).delete(id);
        transaction.oncomplete = () => resolve();
        transaction.onerror = () => reject(transaction.error || new Error("IndexedDB delete failed"));
        transaction.onabort = () => reject(transaction.error || new Error("IndexedDB delete aborted"));
      });
    } finally {
      db.close();
    }
  }

  async function extractText(file, extension) {
    if (extension === ".txt" || extension === ".md") {
      return (await file.text()).slice(0, MAX_TEXT_CHARS);
    }

    if (extension === ".pdf") {
      if (!pdfModulePromise) {
        pdfModulePromise = import("https://cdn.jsdelivr.net/npm/pdfjs-dist@6.5.438/build/pdf.mjs")
          .then((pdfjs) => {
            pdfjs.GlobalWorkerOptions.workerSrc =
              "https://cdn.jsdelivr.net/npm/pdfjs-dist@6.5.438/build/pdf.worker.mjs";
            return pdfjs;
          })
          .catch((error) => {
            pdfModulePromise = null;
            throw error;
          });
      }
      const pdfjs = await pdfModulePromise;
      const pdf = await pdfjs.getDocument({ data: new Uint8Array(await file.arrayBuffer()) }).promise;
      const pages = [];
      let characterCount = 0;

      for (let pageNumber = 1; pageNumber <= pdf.numPages && characterCount < MAX_TEXT_CHARS; pageNumber += 1) {
        const page = await pdf.getPage(pageNumber);
        const content = await page.getTextContent();
        const pageText = content.items
          .map((item) => (typeof item.str === "string" ? item.str : ""))
          .filter(Boolean)
          .join(" ");
        if (pageText) {
          const remaining = MAX_TEXT_CHARS - characterCount;
          const clipped = pageText.slice(0, remaining);
          pages.push(clipped);
          characterCount += clipped.length;
        }
      }
      return pages.join("\n\n").slice(0, MAX_TEXT_CHARS);
    }

    if (extension === ".docx") {
      const mammoth = await loadScript(
        "https://cdnjs.cloudflare.com/ajax/libs/mammoth/1.13.0/mammoth.browser.min.js",
        "mammoth"
      );
      const result = await mammoth.extractRawText({ arrayBuffer: await file.arrayBuffer() });
      return String(result.value || "").slice(0, MAX_TEXT_CHARS);
    }

    throw new Error(message("unsupported"));
  }

  function makeStorageKey() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();

    const fileInput = form.querySelector('input[type="file"]');
    const file = fileInput && fileInput.files ? fileInput.files[0] : null;
    if (!file) {
      setStatus(message("chooseFile"), true);
      return;
    }
    if (file.size < 1 || file.size > MAX_FILE_BYTES) {
      setStatus(message("tooLarge"), true);
      return;
    }

    const filename = file.name.replace(/\\/g, "/").split("/").pop();
    const extension = filename.slice(filename.lastIndexOf(".")).toLowerCase();
    if (![".pdf", ".docx", ".txt", ".md"].includes(extension)) {
      setStatus(message("unsupported"), true);
      return;
    }

    const submitButton = form.querySelector('button[type="submit"]');
    if (submitButton) submitButton.disabled = true;

    let storageKey = null;
    let definitiveClientRejection = false;
    try {
      setStatus(message("parsing"));
      let extractedText;
      try {
        extractedText = await extractText(file, extension);
      } catch (error) {
        if (error && error.message === message("unsupported")) throw error;
        if (extension === ".pdf") {
          throw new Error(isFrench
            ? "Le PDF n’a pas pu être lu. Vérifiez qu’il n’est pas protégé par mot de passe ou endommagé."
            : "The PDF could not be read. Check that it is not password-protected or damaged.");
        }
        if (extension === ".docx") {
          throw new Error(isFrench
            ? "Le document DOCX n’a pas pu être lu. Vérifiez que le fichier n’est pas endommagé."
            : "The DOCX document could not be read. Check that the file is not damaged.");
        }
        throw error;
      }

      const hasExtractedText = Boolean(extractedText.trim());
      if (!hasExtractedText) {
        extractedText = isFrench
          ? "[Aucun texte sélectionnable. Le document original est conservé dans le navigateur. Un PDF numérisé peut nécessiter une reconnaissance optique.]"
          : "[No selectable text was found. The original document is retained in this browser. A scanned PDF may require OCR.]";
      }

      storageKey = makeStorageKey();
      setStatus(message("saving"));
      await idbWrite({
        id: storageKey,
        filename,
        contentType: file.type || "application/octet-stream",
        size: file.size,
        uploadedAt: new Date().toISOString(),
        blob: file
      });

      const payload = {
        csrf: form.elements.csrf.value,
        filename,
        document_type: form.elements.document_type.value,
        project_id: form.elements.project_id.value || null,
        browser_storage_key: storageKey,
        file_size: file.size,
        extracted_text: extractedText
      };

      const response = await fetch(form.action, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json"
        },
        body: JSON.stringify(payload)
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        definitiveClientRejection = response.status >= 400 && response.status < 500;
        const detail = result.detail || result.message || message("uploadFailed");
        throw new Error(typeof detail === "string" ? detail : message("uploadFailed"));
      }

      setStatus(hasExtractedText ? message("success") : message("noText"));
      window.location.assign(result.redirect_url || "/app/compliance");
    } catch (error) {
      // Delete the local copy only when the server explicitly rejected the
      // request. Keep it after network/5xx failures because the database commit
      // may have succeeded before the response was interrupted.
      if (storageKey && definitiveClientRejection) {
        try { await idbDelete(storageKey); } catch (_cleanupError) { /* Keep the original error visible. */ }
      }
      setStatus(error && error.message ? error.message : message("genericError"), true);
      if (submitButton) submitButton.disabled = false;
    }
  });

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-indexeddb-download]");
    if (!button) return;

    const id = button.getAttribute("data-indexeddb-download");
    if (!id) return;

    button.disabled = true;
    try {
      const record = await idbRead(id);
      if (!record || !record.blob) {
        setStatus(message("missingLocal"), true);
        return;
      }
      const objectUrl = URL.createObjectURL(record.blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = record.filename || "wai-document";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    } catch (_error) {
      setStatus(message("downloadFailed"), true);
    } finally {
      button.disabled = false;
    }
  });
})();
