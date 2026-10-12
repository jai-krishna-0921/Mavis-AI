// Copyright (c) 2026 Jai Krishna. All rights reserved.
// Proprietary. Use is governed by LICENSE in the repository root.
import { setupServer } from 'msw/node'
import { handlers } from './handlers'

export const server = setupServer(...handlers)
