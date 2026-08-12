import { resolveCardImageUrl } from '../utils/imageUrl'
import { getCardSetNumber } from './card-system'
import CardImage from './CardImage'
import Modal from './ui/Modal'

/**
 * CardImageDialog — a card's artwork at reading size.
 *
 * Built on the shared Modal rather than a bespoke overlay: centred dialog on
 * desktop, bottom sheet on mobile, with the focus trap, Escape handling and
 * scroll lock already solved there.
 *
 * `image` is the caller's grid thumbnail and is only a fallback. Public
 * payloads carry the proxied small artwork because that is what a tile needs;
 * asking resolveCardImageUrl for the large print gets the bigger image from the
 * same endpoint instead of upscaling the thumbnail.
 */
export default function CardImageDialog({ card, image, onClose }) {
  if (!card) return null

  const setNumber = getCardSetNumber(card)
  const meta = [setNumber, card.rarity].filter(Boolean).join(' · ')

  return (
    <Modal isOpen onClose={onClose} title={card.name} size="md">
      <div className="space-y-3 p-4 sm:p-5">
        {/* Width-driven, so the aspect ratio always holds. Setting the height
            instead looks fine until a narrow phone clamps the width: CSS keeps
            the explicit height, the box stops matching the card, and it
            overflows the sheet. The cap is in dvh to match the sheet's own
            max-h-[85dvh] and leave room for its header and this caption. */}
        <div className="mx-auto aspect-[2.5/3.5] w-full max-w-[min(22rem,40dvh)] overflow-hidden rounded-xl border border-white/10 bg-bg-primary/50">
          <CardImage
            src={resolveCardImageUrl(card, 'large') || image}
            alt={card.name}
            className="h-full w-full object-contain"
            loading="eager"
          />
        </div>
        {meta && <p className="text-center text-xs text-text-muted">{meta}</p>}
      </div>
    </Modal>
  )
}
