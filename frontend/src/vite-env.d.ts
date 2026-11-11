/// <reference types="vite/client" />
declare module "monaco-editor/languages/definitions/cpp/cpp.js";
declare module "monaco-esm/editor/editor.worker.js?worker" {
  const W: { new (): Worker };
  export default W;
}
declare module "monaco-esm/*";
