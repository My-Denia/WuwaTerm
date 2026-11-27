/**
 * Zero-dependency ESM wrapper for `wuwaterm_text_kernel_wasm.wasm`.
 * Works identically in Browsers, Web Workers, and Node.js.
 */

const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8");

export const SEGMENT_LIMIT = 1800;

export class WuwaTermKernelError extends Error {
  constructor(message) {
    super(message);
    this.name = "WuwaTermKernelError";
  }
}

export class TermAutomatonHandle {
  /**
   * @param {WuwaTermTextKernel} kernel
   * @param {number} handle
   * @param {{ pattern_count: number, state_count: number }} stats
   */
  constructor(kernel, handle, stats) {
    this._kernel = kernel;
    this._handle = handle;
    this.patternCount = stats.pattern_count;
    this.stateCount = stats.state_count;
    this._freed = false;
  }

  /**
   * @param {string} text
   * @param {{ select?: boolean, includeUtf16?: boolean }} [options]
   */
  scan(text, options = {}) {
    if (this._freed) {
      throw new WuwaTermKernelError("TermAutomatonHandle has been freed");
    }
    return this._kernel._scanWithHandle(this._handle, text, options);
  }

  free() {
    if (!this._freed) {
      this._kernel._freeHandle(this._handle);
      this._freed = true;
    }
  }
}

export class WuwaTermTextKernel {
  /**
   * @param {WebAssembly.Instance} instance
   */
  constructor(instance) {
    this._exports = instance.exports;
    this._memory = /** @type {WebAssembly.Memory} */ (this._exports.memory);
  }

  _readResultString() {
    const ptr = this._exports.wasm_result_ptr();
    const len = this._exports.wasm_result_len();
    if (len === 0) {
      return "";
    }
    const view = new Uint8Array(this._memory.buffer, ptr, len);
    return decoder.decode(view);
  }

  _callJson(fnName, payload) {
    const encoded = encoder.encode(JSON.stringify(payload));
    const ptr = this._exports.wasm_alloc(encoded.length);
    if (ptr === 0) {
      throw new WuwaTermKernelError("WASM allocation failed");
    }
    try {
      new Uint8Array(this._memory.buffer, ptr, encoded.length).set(encoded);
      const status = this._exports[fnName](ptr, encoded.length);
      const text = this._readResultString();
      if (status !== 0) {
        throw new WuwaTermKernelError(text || `${fnName} failed with status ${status}`);
      }
      return text ? JSON.parse(text) : null;
    } finally {
      this._exports.wasm_dealloc(ptr, encoded.length);
    }
  }

  version() {
    const status = this._exports.wasm_version();
    const text = this._readResultString();
    if (status !== 0) {
      throw new WuwaTermKernelError(text);
    }
    return JSON.parse(text);
  }

  textSpans(text) {
    return this._callJson("wasm_text_spans", { text });
  }

  segmentText(text, limit = SEGMENT_LIMIT, options = {}) {
    return this._callJson("wasm_segment_text", {
      text,
      limit,
      include_utf16: Boolean(options.includeUtf16),
    });
  }

  pairSegments(source, target, limit = SEGMENT_LIMIT, alignments = null, options = {}) {
    return this._callJson("wasm_pair_segments", {
      source,
      target,
      limit,
      alignments,
      include_utf16: Boolean(options.includeUtf16),
    });
  }

  scanTerms(surfaces, text, options = {}) {
    return this._callJson("wasm_scan_terms", {
      surfaces,
      text,
      select: options.select ?? true,
      include_utf16: Boolean(options.includeUtf16),
    });
  }

  createTermAutomaton(surfaces) {
    const encoded = encoder.encode(JSON.stringify(surfaces));
    const ptr = this._exports.wasm_alloc(encoded.length);
    if (ptr === 0) {
      throw new WuwaTermKernelError("WASM allocation failed");
    }
    let handle = 0;
    try {
      new Uint8Array(this._memory.buffer, ptr, encoded.length).set(encoded);
      handle = this._exports.wasm_ac_create(ptr, encoded.length);
      const text = this._readResultString();
      if (handle === 0) {
        throw new WuwaTermKernelError(text || "wasm_ac_create failed");
      }
      const stats = JSON.parse(text);
      const result = new TermAutomatonHandle(this, handle, stats);
      handle = 0;
      return result;
    } catch (err) {
      if (handle !== 0) {
        try {
          this._exports.wasm_ac_free(handle);
        } catch {
          // Best-effort cleanup of newly created handle on parse/init failure.
        }
      }
      throw err;
    } finally {
      this._exports.wasm_dealloc(ptr, encoded.length);
    }
  }

  _scanWithHandle(handle, text, options = {}) {
    const encoded = encoder.encode(
      JSON.stringify({
        text,
        select: options.select ?? true,
        include_utf16: Boolean(options.includeUtf16),
      }),
    );
    const ptr = this._exports.wasm_alloc(encoded.length);
    if (ptr === 0) {
      throw new WuwaTermKernelError("WASM allocation failed");
    }
    try {
      new Uint8Array(this._memory.buffer, ptr, encoded.length).set(encoded);
      const status = this._exports.wasm_ac_scan_terms(handle, ptr, encoded.length);
      const out = this._readResultString();
      if (status !== 0) {
        throw new WuwaTermKernelError(out || `wasm_ac_scan_terms failed (${status})`);
      }
      return JSON.parse(out);
    } finally {
      this._exports.wasm_dealloc(ptr, encoded.length);
    }
  }

  _freeHandle(handle) {
    const status = this._exports.wasm_ac_free(handle);
    if (status !== 0) {
      throw new WuwaTermKernelError(this._readResultString());
    }
  }

  scalarToUtf16(text, scalar) {
    return this._callJson("wasm_scalar_to_utf16", { text, scalar }).utf16;
  }

  utf16ToScalar(text, utf16) {
    return this._callJson("wasm_utf16_to_scalar", { text, utf16 }).scalar;
  }

  coordinateMaps(text) {
    return this._callJson("wasm_coordinate_maps", { text });
  }
}

/**
 * Instantiate the WuwaTerm WASM text kernel from raw bytes, ArrayBuffer,
 * WebAssembly.Module, or a Fetch Response.
 *
 * @param {BufferSource | WebAssembly.Module | Response | Promise<Response>} source
 * @returns {Promise<WuwaTermTextKernel>}
 */
export async function loadKernel(source) {
  const resolved = await source;
  if (resolved instanceof WebAssembly.Module) {
    const instance = await WebAssembly.instantiate(resolved, {});
    return new WuwaTermTextKernel(instance);
  }
  if (typeof Response !== "undefined" && resolved instanceof Response) {
    if (typeof WebAssembly.instantiateStreaming === "function") {
      try {
        const { instance } = await WebAssembly.instantiateStreaming(resolved.clone(), {});
        return new WuwaTermTextKernel(instance);
      } catch {
        // Fallback when server Content-Type is not application/wasm
      }
    }
    const bytes = await resolved.arrayBuffer();
    const { instance } = await WebAssembly.instantiate(bytes, {});
    return new WuwaTermTextKernel(instance);
  }
  const { instance } = await WebAssembly.instantiate(resolved, {});
  return new WuwaTermTextKernel(instance);
}
