import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ConnectorIcon } from './ConnectorIcon'

describe('ConnectorIcon', () => {
  it('renders an svg for known and unknown ids', () => {
    for (const id of ['google', 'slack', 'testco']) {
      const { container, unmount } = render(<ConnectorIcon icon={id} />)
      expect(container.querySelector('svg')).not.toBeNull()
      unmount()
    }
  })
})
