// Copyright (c) 2026 Jai Krishna. All rights reserved.
// Proprietary. Use is governed by LICENSE in the repository root.
// Full page navigation (OAuth redirects). Isolated so tests can spy on it.
export const redirectTo = (url: string) => { window.location.assign(url) }
