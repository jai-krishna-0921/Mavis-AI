// Copyright (c) 2026 Jai Krishna. All rights reserved.
// Proprietary. Use is governed by LICENSE in the repository root.
import gsap from 'gsap'
import { ScrollTrigger } from 'gsap/ScrollTrigger'
import { useGSAP } from '@gsap/react'

gsap.registerPlugin(ScrollTrigger, useGSAP)

// Scroll animations only run with motion allowed. `desktop` also needs room for a pinned column.
export const MOTION = '(prefers-reduced-motion: no-preference)'
export const MOTION_DESKTOP = '(min-width: 1024px) and (prefers-reduced-motion: no-preference)'

export { gsap, ScrollTrigger, useGSAP }
