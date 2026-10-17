import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ConnectorIcon } from './ConnectorIcon'

describe('ConnectorIcon', () => {
  it('renders an svg for known and unknown ids', () => {
    for (const id of ['google', 'slack', 'microsoft', 'testco']) {
      const { container, unmount } = render(<ConnectorIcon icon={id} />)
      expect(container.querySelector('svg')).not.toBeNull()
      unmount()
    }
  })

  it('renders notion and fathom with their own icons, not the fallback plug', () => {
    const svg = (id: string) => {
      const { container, unmount } = render(<ConnectorIcon icon={id} />)
      const html = container.innerHTML
      unmount()
      return html
    }
    const fallback = svg('testco')
    for (const id of ['notion', 'fathom']) expect(svg(id)).not.toBe(fallback)
    expect(svg('notion')).not.toBe(svg('fathom'))
  })
})
