// Mirrors what @decky/rollup does, built only from packages the loader's frontend already has:
// React, ReactDOM and @decky/ui come from the loader's globals, and '@decky/manifest' is plugin.json inlined.
import { readFileSync } from 'fs';
import commonjs from '@rollup/plugin-commonjs';
import json from '@rollup/plugin-json';
import { nodeResolve } from '@rollup/plugin-node-resolve';
import replace from '@rollup/plugin-replace';
import typescript from '@rollup/plugin-typescript';
import { defineConfig } from 'rollup';
import del from 'rollup-plugin-delete';
import externalGlobals from 'rollup-plugin-external-globals';

const manifest = JSON.parse(readFileSync('plugin.json', 'utf-8'));

const MANIFEST_ID = '@decky/manifest';
const manifestPlugin = {
  name: 'decky-manifest',
  resolveId: (id) => (id === MANIFEST_ID ? '\0' + MANIFEST_ID : null),
  load: (id) => (id === '\0' + MANIFEST_ID ? `export default ${JSON.stringify({ name: manifest.name })};` : null),
};

export default defineConfig({
  input: 'src/index.tsx',
  plugins: [
    del({ targets: ['dist/*'] }),
    manifestPlugin,
    commonjs(),
    nodeResolve({ browser: true }),
    json(),
    externalGlobals({
      react: 'SP_REACT',
      'react/jsx-runtime': 'SP_JSX',
      'react-dom': 'SP_REACTDOM',
      '@decky/ui': 'DFL',
    }),
    typescript(),
    replace({
      preventAssignment: false,
      'process.env.NODE_ENV': JSON.stringify('production'),
    }),
  ],
  context: 'window',
  external: ['react', 'react-dom', '@decky/ui'],
  treeshake: {
    pureExternalImports: { pure: ['@decky/ui', '@decky/api'] },
    preset: 'smallest',
  },
  output: {
    file: 'dist/index.js',
    format: 'esm',
    sourcemap: true,
    sourcemapPathTransform: (relativeSourcePath) =>
      relativeSourcePath.replace(/^\.\.\//, `decky://decky/plugin/${encodeURIComponent(manifest.name)}/`),
    exports: 'default',
  },
});
