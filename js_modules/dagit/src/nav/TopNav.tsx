import {Breadcrumbs, Breadcrumb, Colors, IBreadcrumbProps, Tabs, Tab} from '@blueprintjs/core';
import * as React from 'react';
import {Link} from 'react-router-dom';
import styled from 'styled-components/macro';

export interface TopNavProps {
  activeTab?: string;
  breadcrumbs: IBreadcrumbProps[];
  tabs?: {text: string; href: string}[];
}

export const TopNav = (props: TopNavProps) => {
  const {activeTab, breadcrumbs, tabs} = props;
  return (
    <Container>
      <BreadcrumbContainer>
        <Breadcrumbs
          breadcrumbRenderer={(props) => <SmallerBreadcrumb {...props} />}
          currentBreadcrumbRenderer={(props) => <CurrentBreadcrumb {...props} />}
          items={breadcrumbs}
        />
      </BreadcrumbContainer>
      {tabs ? (
        <Tabs large={false} selectedTabId={activeTab}>
          {tabs.map((tab) => {
            const {href, text} = tab;
            return <Tab key={text} id={text} title={<Link to={href}>{text}</Link>} />;
          })}
        </Tabs>
      ) : null}
    </Container>
  );
};

const SmallerBreadcrumb = styled(Breadcrumb)`
  font-size: 14px;
`;

const CurrentBreadcrumb = styled(Breadcrumb)`
  font-size: 14px;
  font-weight: 600;
`;

const BreadcrumbContainer = styled.div`
  margin-right: 40px;
`;

const Container = styled.div`
  background: ${Colors.LIGHT_GRAY4};
  border-bottom: 1px solid ${Colors.GRAY5};
  display: flex;
  flex: 0 0 auto;
  flex-wrap: wrap;
  padding: 2px 16px 0;
`;
