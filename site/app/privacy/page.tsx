import { documentMetadata } from '@/lib/public-metadata';
import { msg } from '@/lib/messages';
import { readUiLanguage } from '../ui-language-server';
import { PrivacyBody } from '../components/privacy-body';

export async function generateMetadata() {
  const m = msg(await readUiLanguage());
  return documentMetadata(m.privacy.title, m.privacy.description, '/privacy');
}
export default function Privacy() {
  return <PrivacyBody />;
}
