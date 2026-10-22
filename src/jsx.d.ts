// @types/react 19 has no global JSX namespace, and the classic `window.SP_REACT.createElement` factory cannot point
// TypeScript at React's own, so expose React's intrinsic elements globally.
import type { JSX as ReactJSX } from 'react';

declare global {
  namespace JSX {
    interface IntrinsicElements extends ReactJSX.IntrinsicElements {}
    // Lets `key` be passed to function components in lists.
    interface IntrinsicAttributes extends ReactJSX.IntrinsicAttributes {}
  }
}
