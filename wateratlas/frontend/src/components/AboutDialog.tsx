import { useEffect, useRef } from "react";
import { APP_NAME, AUTHOR, COPYRIGHT_YEAR, RIVRETRIEVE_URL, SOURCE_URL } from "../lib/about";
import { formatCount } from "../lib/palette";

interface Props {
  open: boolean;
  onClose: () => void;
  stationCount: number;
  providerCount: number;
}

/** What the atlas is, whose data it shows, and who to contact. A native modal <dialog>. */
export function AboutDialog({ open, onClose, stationCount, providerCount }: Props) {
  const ref = useRef<HTMLDialogElement>(null);

  // The browser owns focus trapping, Escape and the backdrop; React only says open or closed.
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      className="riv-about"
      aria-labelledby="riv-about-title"
      onClose={onClose}
      // The dialog box is filled by its content, so a click on the element itself is the backdrop.
      onClick={(event) => {
        if (event.target === ref.current) onClose();
      }}
    >
      <div className="riv-about-body">
        <header className="riv-about-head">
          <div>
            <div className="riv-eyebrow">About</div>
            <h2 id="riv-about-title">{APP_NAME}</h2>
          </div>
          <button type="button" className="riv-icon-button" aria-label="Close" onClick={onClose}>
            ×
          </button>
        </header>

        <p>
          {APP_NAME} maps observed water data from around the world: river discharge and water level,
          rainfall, reservoir levels, water temperature, and coastal and Great Lakes water levels. It
          brings together {stationCount > 0 ? formatCount(stationCount) : "tens of thousands of"}{" "}
          stations from {providerCount > 0 ? providerCount : "many"} public data providers, so you can
          find stations, preview their records and download them as CSV in SI units.
        </p>
        <p className="riv-muted">
          Each series is fetched on request from its provider through the open-source{" "}
          <a href={RIVRETRIEVE_URL} target="_blank" rel="noreferrer">
            RivRetrieve
          </a>{" "}
          library. All data rights remain with the original providers; check their terms before
          redistributing. Every download includes an <code>ATTRIBUTION.md</code> naming its sources.
        </p>

        <section aria-labelledby="riv-about-contact">
          <h3 id="riv-about-contact" className="riv-about-label">
            Contact
          </h3>
          <address className="riv-about-contact">
            <strong>{AUTHOR.name}</strong>
            <span>{AUTHOR.organisation}</span>
            <span>{AUTHOR.department}</span>
            <span>
              {AUTHOR.postcode} {AUTHOR.address}
            </span>
            <span>
              E-mail: <a href={`mailto:${AUTHOR.email}`}>{AUTHOR.email}</a>
            </span>
          </address>
        </section>

        <footer className="riv-about-foot">
          <span>
            © {COPYRIGHT_YEAR} {AUTHOR.shortName}. Built on RivRetrieve (MIT licence).
          </span>
          <a href={SOURCE_URL} target="_blank" rel="noreferrer">
            Source code
          </a>
        </footer>
      </div>
    </dialog>
  );
}
