import { documentMetadata } from '@/lib/public-metadata';
import { msg } from '@/lib/messages';
import { readUiLanguage } from '../ui-language-server';
import { LimitsBody } from '../components/limits-body';

export async function generateMetadata() {
  const m = msg(await readUiLanguage());
  return documentMetadata(m.limits.title, m.limits.description, '/limits');
}
export default function Limits() {
  return <LimitsBody />;
}
