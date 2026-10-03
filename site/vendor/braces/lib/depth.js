'use strict';

// This ceiling covers parser nesting and every recursive AST walker, including
// caller-provided ASTs. A custom limit may only make it stricter.
const MAX_DEPTH = 100;

exports.checkDepth = (depth, options = {}) => {
  const requested = options && options.maxDepth;
  const limit = typeof requested === 'number' && Number.isFinite(requested) && requested >= 0
    ? Math.min(MAX_DEPTH, Math.floor(requested))
    : MAX_DEPTH;
  if (depth > limit) {
    throw new SyntaxError(`Brace nesting exceeds maximum depth (${limit})`);
  }
};

// Parser-produced values are strings. Validate direct AST inputs iteratively
// before a walker can coerce an array value (Array.toString is recursive too).
// Follow children only: normal ASTs contain parent/prev backreferences.
exports.validateTree = (ast, options = {}) => {
  const pending = [{ node: ast, depth: 0 }];
  while (pending.length) {
    const { node, depth } = pending.pop();
    if (!node || typeof node !== 'object' || Array.isArray(node)) {
      throw new TypeError('Expected an AST node');
    }
    if (node.value !== undefined && typeof node.value !== 'string') {
      throw new TypeError('AST node value must be a string');
    }
    if (node.nodes !== undefined) {
      if (!Array.isArray(node.nodes)) throw new TypeError('AST children must be an array');
      exports.checkDepth(depth, options);
      for (const child of node.nodes) pending.push({ node: child, depth: depth + 1 });
    }
  }
};
