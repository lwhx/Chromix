/* Observations only: no spoofing, permission requests, or device substitution. */
globalThis.chromixDeviceProbe = async () => {
  const capture = async fn => {
    try { return {status:'observed', value:await fn()}; }
    catch (e) { return {status:'error', reason:e.name + ': ' + e.message}; }
  };
  const absent = reason => ({status:'unavailable', reason});
  const require = (condition, message) => { if (!condition) throw Error(message); };
  const n = navigator;
  const execution = {
    cores:n.hardwareConcurrency, deviceMemory:n.deviceMemory ?? null,
    wasm:typeof WebAssembly === 'object', sab:typeof SharedArrayBuffer === 'function',
    isolated:globalThis.crossOriginIsolated === true,
    heapLimit:globalThis.performance?.memory?.jsHeapSizeLimit ?? null,
  };
  if (execution.wasm) {
    const simd = new Uint8Array([0,97,115,109,1,0,0,0,1,5,1,96,0,1,127,
      3,2,1,0,7,7,1,3,114,117,110,0,0,10,11,1,9,0,65,7,253,15,253,21,0,11]);
    execution.simd = WebAssembly.validate(simd);
    execution.runtime = await capture(async () => {
      const scalar = new Uint8Array([0,97,115,109,1,0,0,0,1,5,1,96,0,1,127,
        3,2,1,0,7,7,1,3,114,117,110,0,0,10,6,1,4,0,65,42,11]);
      const module = await WebAssembly.instantiate(scalar);
      require(module.instance.exports.run() === 42, 'Wasm execution mismatch');
      if (execution.simd) {
        const module = await WebAssembly.instantiate(simd);
        require(module.instance.exports.run() === 7, 'SIMD execution mismatch');
      }
      const memory = new WebAssembly.Memory({initial:1, maximum:2});
      new Uint8Array(memory.buffer)[0] = 73;
      require(memory.grow(1) === 1 && memory.buffer.byteLength === 131072 &&
        new Uint8Array(memory.buffer)[0] === 73, 'Wasm memory growth mismatch');
      let bounded = false;
      try { memory.grow(1); } catch (e) { bounded = e instanceof RangeError; }
      require(bounded, 'Wasm maximum not enforced');
      let atomics = null;
      if (execution.sab) {
        const values = new Int32Array(new SharedArrayBuffer(4));
        require(Atomics.add(values, 0, 7) === 0 && Atomics.load(values, 0) === 7,
          'Atomics mismatch');
        atomics = true;
      }
      return {scalar:42, simd:execution.simd ? 7 : null, memoryGrowth:true, maximumEnforced:true, atomics};
    });
    execution.sharedMemory = await capture(() => {
      const memory = new WebAssembly.Memory({initial:1, maximum:1, shared:true});
      return typeof SharedArrayBuffer === 'function' && memory.buffer instanceof SharedArrayBuffer;
    });
  }
  const result = {probeVersion:2, execution, identity:await capture(async () => ({
    ua:n.userAgent, platform:n.platform, languages:Array.from(n.languages),
    timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
    locale:Intl.DateTimeFormat().resolvedOptions().locale,
    uaData:n.userAgentData ? await n.userAgentData.getHighEntropyValues([
      'architecture','bitness','platformVersion','fullVersionList','model','wow64']) : null,
  }))};
  result.http = await capture(async () => {
    const response = await fetch('/headers', {cache:'no-store'});
    require(response.ok, 'HTTP echo failed: ' + response.status);
    return response.json();
  });
  result.network = n.connection ? {status:'observed', value:{
    effectiveType:n.connection.effectiveType, rtt:n.connection.rtt,
    downlink:n.connection.downlink, saveData:n.connection.saveData, online:n.onLine,
  }} : absent('Network Information API unavailable');
  result.webgpu = n.gpu ? await capture(async () => {
    const adapter = await n.gpu.requestAdapter();
    if (!adapter) return {adapter:null};
    const info = adapter.info;
    const limits = {};
    for (const key in adapter.limits) if (typeof adapter.limits[key] === 'number') limits[key] = adapter.limits[key];
    const device = await adapter.requestDevice();
    let compute, readback, texture, pixels;
    let backend;
    try {
      device.pushErrorScope('validation');
      compute = device.createBuffer({size:4, usage:GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_SRC});
      readback = device.createBuffer({size:4, usage:GPUBufferUsage.MAP_READ | GPUBufferUsage.COPY_DST});
      const module = device.createShaderModule({code:
        '@group(0) @binding(0) var<storage, read_write> output: array<u32>; @compute @workgroup_size(1) fn main() { output[0] = 42u; }'});
      const pipeline = await device.createComputePipelineAsync({layout:'auto', compute:{module, entryPoint:'main'}});
      const group = device.createBindGroup({layout:pipeline.getBindGroupLayout(0), entries:[{binding:0, resource:{buffer:compute}}]});
      texture = device.createTexture({size:[1,1], format:'rgba8unorm',
        usage:GPUTextureUsage.RENDER_ATTACHMENT | GPUTextureUsage.COPY_SRC});
      pixels = device.createBuffer({size:256, usage:GPUBufferUsage.MAP_READ | GPUBufferUsage.COPY_DST});
      const commands = device.createCommandEncoder();
      const pass = commands.beginComputePass();
      pass.setPipeline(pipeline); pass.setBindGroup(0, group); pass.dispatchWorkgroups(1); pass.end();
      commands.copyBufferToBuffer(compute, 0, readback, 0, 4);
      const render = commands.beginRenderPass({colorAttachments:[{view:texture.createView(),
        loadOp:'clear', storeOp:'store', clearValue:{r:0,g:1,b:0,a:1}}]});
      render.end();
      commands.copyTextureToBuffer({texture}, {buffer:pixels, bytesPerRow:256}, [1,1]);
      device.queue.submit([commands.finish()]);
      await Promise.all([readback.mapAsync(GPUMapMode.READ), pixels.mapAsync(GPUMapMode.READ)]);
      const word = new Uint32Array(readback.getMappedRange())[0];
      const rgba = Array.from(new Uint8Array(pixels.getMappedRange(), 0, 4));
      require(word === 42 && rgba.join(',') === '0,255,0,255', 'WebGPU readback mismatch');
      const error = await device.popErrorScope();
      require(!error, error?.message);
      backend = {compute:word, textureRGBA:rgba, mapRead:true};
    } finally {
      for (const resource of [compute, readback, texture, pixels]) resource?.destroy();
      device.destroy();
    }
    return {info:info ? {vendor:info.vendor, architecture:info.architecture,
      device:info.device, description:info.description, isFallbackAdapter:info.isFallbackAdapter ?? null} : null,
      features:Array.from(adapter.features).sort(), limits, requestDevice:true, backend,
      preferredFormat:n.gpu.getPreferredCanvasFormat()};
  }) : absent('WebGPU unavailable');
  const makeCanvas = () => {
    if (typeof document !== 'undefined') return document.createElement('canvas');
    return new OffscreenCanvas(32, 32);
  };
  result.webgl = typeof document !== 'undefined' || typeof OffscreenCanvas !== 'undefined' ?
    await capture(() => {
      const gl = makeCanvas().getContext('webgl2');
      if (!gl) return {context:null};
      try {
        const ext = gl.getExtension('WEBGL_debug_renderer_info');
        const limits = {};
        for (const key of ['MAX_TEXTURE_SIZE','MAX_RENDERBUFFER_SIZE','MAX_VERTEX_ATTRIBS',
          'MAX_VERTEX_UNIFORM_COMPONENTS','MAX_FRAGMENT_UNIFORM_COMPONENTS','MAX_SAMPLES'])
          limits[key] = gl.getParameter(gl[key]);
        const shader = (type, source) => {
          const value = gl.createShader(type);
          gl.shaderSource(value, source); gl.compileShader(value);
          require(gl.getShaderParameter(value, gl.COMPILE_STATUS), gl.getShaderInfoLog(value));
          return value;
        };
        const program = gl.createProgram();
        gl.attachShader(program, shader(gl.VERTEX_SHADER, '#version 300 es\nvoid main(){vec2 p=vec2(float((gl_VertexID<<1)&2),float(gl_VertexID&2));gl_Position=vec4(p*2.0-1.0,0.0,1.0);}'));
        gl.attachShader(program, shader(gl.FRAGMENT_SHADER, '#version 300 es\nprecision highp float;out vec4 color;void main(){color=vec4(0.0,1.0,0.0,1.0);}'));
        gl.linkProgram(program);
        require(gl.getProgramParameter(program, gl.LINK_STATUS), gl.getProgramInfoLog(program));
        const texture = gl.createTexture();
        gl.bindTexture(gl.TEXTURE_2D, texture);
        gl.texStorage2D(gl.TEXTURE_2D, 1, gl.RGBA8, 1, 1);
        const framebuffer = gl.createFramebuffer();
        gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer);
        gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texture, 0);
        require(gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE, 'Incomplete framebuffer');
        gl.viewport(0,0,1,1); gl.disable(gl.DITHER); gl.useProgram(program); gl.drawArrays(gl.TRIANGLES,0,3);
        const pixel = new Uint8Array(4);
        gl.readPixels(0,0,1,1,gl.RGBA,gl.UNSIGNED_BYTE,pixel);
        require(gl.getError() === gl.NO_ERROR && pixel.join(',') === '0,255,0,255', 'WebGL shader/texture readback mismatch');
        const extensions = gl.getSupportedExtensions().sort();
        const unrequestable = extensions.filter(name => !gl.getExtension(name));
        require(unrequestable.length === 0, 'Enumerated extensions cannot be requested: ' + unrequestable);
        return {vendor:ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : null,
          renderer:ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null,
          version:gl.getParameter(gl.VERSION), limits, extensions,
          backend:{shader:true, textureRGBA:Array.from(pixel), requestExtensions:true}};
      } finally { gl.getExtension('WEBGL_lose_context')?.loseContext(); }
    }) : absent('Canvas unavailable');
  if (typeof document === 'undefined') return result;
  const s = screen, v = visualViewport;
  result.display = {screen:{width:s.width, height:s.height, availWidth:s.availWidth,
    availHeight:s.availHeight, availLeft:s.availLeft, availTop:s.availTop,
    colorDepth:s.colorDepth, pixelDepth:s.pixelDepth, orientation:s.orientation?.type},
    window:{x:screenX, y:screenY, outerWidth, outerHeight, innerWidth, innerHeight},
    viewport:v ? {width:v.width, height:v.height, scale:v.scale, offsetLeft:v.offsetLeft, offsetTop:v.offsetTop} : null,
    dpr:devicePixelRatio, css:{
      deviceWidth:matchMedia(`(device-width: ${s.width}px)`).matches,
      deviceHeight:matchMedia(`(device-height: ${s.height}px)`).matches,
      resolution:matchMedia(`(resolution: ${devicePixelRatio}dppx)`).matches,
      pointer:['none','coarse','fine'].filter(x => matchMedia(`(pointer: ${x})`).matches),
      hover:matchMedia('(hover: hover)').matches,
    }};
  result.fonts = await capture(async () => {
    await document.fonts.ready;
    const canvas = makeCanvas(), context = canvas.getContext('2d');
    const widths = {}, samples = [];
    const texts = ['Aa09', '\u4e2d\u6587', '\u{1f600}', '\u2211', '\u0378'];
    for (const family of ['serif','sans-serif','monospace','system-ui']) {
      context.font = `16px ${family}`;
      widths[family] = context.measureText('Aa09 \u4e2d\u6587 \u{1f600} \u2211').width;
      for (const text of texts) {
        const span = document.createElement('span');
        span.textContent = text;
        // Inline styles may be blocked by the origin CSP; use presentation attributes via CSSOM.
        span.style.cssText = `position:absolute;white-space:pre;font:16px ${family};font-kerning:none;font-variant-ligatures:none;`;
        document.body.appendChild(span);
        try {
          context.fontKerning = 'none';
          const dom = span.getBoundingClientRect().width, canvasWidth = context.measureText(text).width;
          const loaded = document.fonts.check(`16px ${family}`, text);
          require(Math.abs(dom - canvasWidth) <= 1, 'CSS/Canvas font metrics mismatch');
          samples.push({family,text,domWidth:dom,canvasWidth,loaded});
        } finally { span.remove(); }
      }
    }
    return {genericWidths:widths, samples, provenance:'Glyph source files are not exposed by this probe'};
  });
  result.media = n.mediaDevices ? await capture(async () => ({
    devices:(await n.mediaDevices.enumerateDevices()).map(d => ({kind:d.kind,
      label:d.label, deviceId:d.deviceId, groupId:d.groupId})),
    constraints:n.mediaDevices.getSupportedConstraints(), captureTested:false,
  })) : absent('MediaDevices unavailable');
  result.audio = typeof AudioContext === 'function' ? await capture(async () => {
    const context = new AudioContext();
    try { return {sampleRate:context.sampleRate, baseLatency:context.baseLatency,
      outputLatency:context.outputLatency ?? null, maxChannelCount:context.destination.maxChannelCount,
      state:context.state, playbackTested:false}; }
    finally { await context.close(); }
  }) : absent('AudioContext unavailable');
  return result;
};
