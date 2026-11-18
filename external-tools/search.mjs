// The upstream default also opens an HTTP listener. Use its stdio-only mode.
process.env.MODE = 'stdio';
process.env.SEARCH_MODE = 'request';
process.env.ALLOWED_SEARCH_ENGINES = 'bing';
await import('./node_modules/open-websearch/build/index.js');
