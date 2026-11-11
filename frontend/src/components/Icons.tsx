/** Small inline SVG icons: they look the same in every browser (no dependency on emoji / CJK fonts). */
const S = ({ children, title, size = 14 }: { children: React.ReactNode; title?: string; size?: number }) => (
  <svg className="ui-icon" viewBox="0 0 16 16" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden={title ? undefined : true} role={title ? "img" : undefined}>
    {title && <title>{title}</title>}
    {children}
  </svg>
);
export const PlusIcon = (p: { size?: number }) => <S {...p}><path d="M8 3v10M3 8h10" /></S>;
export const MinusIcon = (p: { size?: number }) => <S {...p}><path d="M3 8h10" /></S>;
export const UserIcon = (p: { size?: number }) => <S {...p}><circle cx="8" cy="5.5" r="2.7" /><path d="M2.8 14c.6-2.8 2.7-4.3 5.2-4.3s4.6 1.5 5.2 4.3" /></S>;
export const NewFileIcon = (p: { size?: number }) => <S {...p}><path d="M9 1.8H4.2a1 1 0 00-1 1v10.4a1 1 0 001 1h7.6a1 1 0 001-1V5.6z" /><path d="M9 1.8v3.8h3.8M8 7.8v4.4M5.8 10h4.4" /></S>;
export const NewFolderIcon = (p: { size?: number }) => <S {...p}><path d="M1.8 4.2a1 1 0 011-1h3.4l1.5 1.6h5.5a1 1 0 011 1v6.9a1 1 0 01-1 1H2.8a1 1 0 01-1-1z" /><path d="M8 7.2v4M6 9.2h4" /></S>;
export const FilesIcon = (p: { size?: number }) => <S {...p}><path d="M2 4a1 1 0 011-1h3.4l1.5 1.6H13a1 1 0 011 1v6.9a1 1 0 01-1 1H3a1 1 0 01-1-1z" /></S>;
export const PullRequestIcon = (p: { size?: number }) => <S {...p}><circle cx="4" cy="3.5" r="1.6" /><circle cx="4" cy="12.5" r="1.6" /><circle cx="12" cy="12.5" r="1.6" /><path d="M4 5.1v5.8M12 10.9V6.3a2 2 0 00-2-2H7.2M8.6 2.8L7 4.3l1.6 1.5" /></S>;
export const ChatIcon = (p: { size?: number }) => <S {...p}><path d="M2.5 3.5a1 1 0 011-1h9a1 1 0 011 1v6.2a1 1 0 01-1 1H7l-3 2.6v-2.6h-.5a1 1 0 01-1-1z" /><path d="M5 5.8h6M5 8h4" /></S>;
export const CodeIcon = (p: { size?: number }) => <S {...p}><path d="M5.5 4.5L2 8l3.5 3.5M10.5 4.5L14 8l-3.5 3.5M9 2.8L7 13.2" /></S>;
export const SearchIcon = (p: { size?: number }) => <S {...p}><circle cx="7" cy="7" r="4.3" /><path d="M10.2 10.2L14 14" /></S>;
export const ProblemsIcon = (p: { size?: number }) => <S {...p}><circle cx="8" cy="8" r="6" /><path d="M8 4.8v3.8M8 11v.2" /></S>;
export const WikiIcon = (p: { size?: number }) => <S {...p}><path d="M2.5 3.2c1.8-.6 3.8-.4 5.5.8 1.7-1.2 3.7-1.4 5.5-.8v9.6c-1.8-.6-3.8-.4-5.5.8-1.7-1.2-3.7-1.4-5.5-.8z" /><path d="M8 4v9.6" /></S>;
export const DashboardIcon = (p: { size?: number }) => <S {...p}><path d="M2 13.5h12" /><path d="M3.5 13.5V9M6.5 13.5V5.5M9.5 13.5V7.5M12.5 13.5V3" /></S>;
